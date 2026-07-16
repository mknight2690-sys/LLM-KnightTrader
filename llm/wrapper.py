"""Multi-provider cloud LLM wrapper — OpenRouter key pool with failover."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from activity_log import log_event
from config import (
    APP_NAME,
    LLM_HTTP_TIMEOUT_SEC,
    NOUS_STEPFUN_BASE_URL,
    NOUS_STEPFUN_KEY_PATH,
    NOUS_STEPFUN_MODEL,
)
from credentials import discover_llm_env_keys, discover_openrouter_keys
from llm.model_registry import FREE_OPENROUTER_MODELS, resolve_model_for_agent, _RotationState

SKIP_MODEL_SUBSTRINGS = ("content-safety", "moderation", "north-mini")
GROQ_MODELS = []
GEMINI_MODELS = []


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    latency_ms: float
    raw: dict[str, Any] = field(default_factory=dict)


class LLMWrapper:
    """OpenRouter free-model rotation with per-agent pinned models + 7-key failover."""

    def __init__(
        self,
        *,
        openrouter_models: list[str] | None = None,
        provider_priority: tuple[str, ...] = ("nous",),
        pool_name: str | None = None,
        nvidia_model: str = NOUS_STEPFUN_MODEL,
    ) -> None:
        self._or_keys = discover_openrouter_keys()
        self._env_keys = discover_llm_env_keys()
        self._pool_name = pool_name
        self._openrouter_models = openrouter_models or FREE_OPENROUTER_MODELS
        self._provider_priority = provider_priority
        self._nvidia_model = nvidia_model
        self._cooldown: dict[str, float] = {}
        self._or_key_idx = 0
        self._model_idx = 0
        self._extra_models: list[str] = []
        log_event(
            "system",
            "LLM pool ready",
            f"provider_priority={provider_priority} pool={pool_name or 'none'} model={nvidia_model}",
            {"pool": self._pool_name, "models": self._openrouter_models},
        )

    def _cooling(self, key: str, seconds: float = 90.0) -> None:
        self._cooldown[key] = time.time() + seconds

    def _is_cooled(self, key: str) -> bool:
        return time.time() < self._cooldown.get(key, 0.0)

    def _next_or_key(self) -> str | None:
        if not self._or_keys:
            return None
        for _ in range(len(self._or_keys)):
            key = self._or_keys[self._or_key_idx % len(self._or_keys)]
            self._or_key_idx += 1
            tag = f"or:{key[:12]}"
            if not self._is_cooled(tag):
                return key
        return self._or_keys[0]

    def _http_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: float | None = None,
        *,
        method: str = "POST",
    ) -> dict[str, Any]:
        http_timeout = LLM_HTTP_TIMEOUT_SEC if timeout is None else timeout
        data = json.dumps(payload).encode("utf-8") if method == "POST" else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            return json.loads(resp.read())

    def _extract_text(self, data: dict[str, Any]) -> str:
        if data.get("error"):
            err = data["error"]
            if isinstance(err, dict):
                raise RuntimeError(f"API error {err.get('code', '')}: {err.get('message', err)}")
            raise RuntimeError(f"API error: {err}")
        if "choices" in data:
            msg = data["choices"][0].get("message") or {}
            text = (msg.get("content") or "").strip()
            if not text:
                reasoning_details = msg.get("reasoning_details") or []
                for detail in reasoning_details:
                    text = (detail.get("text") or "").strip()
                    if text:
                        break
                if not text:
                    text = (msg.get("reasoning") or "").strip()
            if not text:
                raise RuntimeError("empty response")
            return text
        if "candidates" in data:
            parts = data["candidates"][0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts).strip()
            if not text:
                raise RuntimeError("empty response")
            return text
        raise RuntimeError(f"invalid LLM response: {json.dumps(data)[:200]}")

    @staticmethod
    def _looks_like_json(text: str) -> bool:
        t = (text or "").strip()
        if t.startswith("```"):
            t = re.sub(r"^```(?:json)?\s*", "", t)
            t = re.sub(r"\s*```$", "", t).strip()
        return t.startswith("{")

    def _refresh_openrouter_models(self) -> None:
        if not self._or_keys:
            return
        try:
            data = self._http_json(
                "https://openrouter.ai/api/v1/models",
                {},
                {"Authorization": f"Bearer {self._or_keys[0]}", "Content-Type": "application/json"},
                method="GET",
            )
            allowed = set(self._openrouter_models)
            free = [
                m["id"]
                for m in data.get("data", [])
                if "content-safety" not in m.get("id", "").lower()
                and "moderation" not in m.get("id", "").lower()
                and (
                    ":free" in m.get("id", "")
                    or m.get("pricing", {}).get("prompt") in ("0", 0, "0.0")
                )
            ]
            # Strict mode: never add models outside the configured openrouter_models.
            filtered = [m for m in free if m in allowed]
            if filtered:
                self._extra_models = filtered[:8]
        except Exception:
            pass

    def _try_openrouter(
        self,
        messages: list[dict[str, str]],
        max_tokens: int,
        *,
        json_mode: bool = False,
    ) -> LLMResponse:
        primary = [
            m
            for m in dict.fromkeys(self._extra_models + self._openrouter_models)
            if not any(s in m.lower() for s in SKIP_MODEL_SUBSTRINGS)
        ]
        fallback = "openai/gpt-oss-20b:free"
        batches = [primary]
        if fallback not in primary:
            batches.append([fallback])

        for batch in batches:
            for _ in range(max(1, len(self._or_keys))):
                key = self._next_or_key()
                if not key:
                    break
                model = batch[self._model_idx % len(batch)]
                self._model_idx += 1
                tag = f"or:{key[:12]}:{model}"
                if self._is_cooled(tag):
                    continue
                t0 = time.time()
                try:
                    payload: dict[str, Any] = {
                        "model": model,
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "temperature": 0.4,
                    }
                    if json_mode:
                        payload["response_format"] = {"type": "json_object"}
                    data = self._http_json(
                        "https://openrouter.ai/api/v1/chat/completions",
                        payload,
                        {
                            "Authorization": f"Bearer {key}",
                            "Content-Type": "application/json",
                            "HTTP-Referer": "http://localhost:8765",
                            "X-Title": APP_NAME,
                        },
                    )
                    text = self._extract_text(data)
                    low = text.lower()
                    if (
                        "unsafe" in low
                        or "unauthorized advice" in low
                        or low.startswith("user safety:")
                        or "content-safety" in model.lower()
                    ):
                        raise RuntimeError("safety refusal")
                    if json_mode and not self._looks_like_json(text):
                        raise RuntimeError("non-json response")
                    latency = (time.time() - t0) * 1000
                    log_event(
                        "llm",
                        f"OpenRouter {model}",
                        text[:240],
                        {"provider": "openrouter", "model": model, "pool": self._pool_name},
                    )
                    return LLMResponse(text=text, provider="openrouter", model=model, latency_ms=latency, raw=data)
                except urllib.error.HTTPError as exc:
                    if exc.code in (429, 402, 403, 404):
                        self._cooling(tag, 120.0 if exc.code == 429 else 45.0)
                        # 7-key rotation: record failure, try next key/model
                        rot = _RotationState()
                        rot.record_failure(model)
                        continue
                    raise
                except Exception:
                    self._cooling(tag, 30.0)
                    continue
        raise RuntimeError("All OpenRouter keys/models exhausted")

    def _try_groq(
        self,
        messages: list[dict[str, str]],
        max_tokens: int,
        *,
        json_mode: bool = False,
    ) -> LLMResponse:
        key = self._env_keys.get("GROQ_API_KEY")
        if not key:
            raise RuntimeError("no groq key")
        for model in GROQ_MODELS:
            tag = f"groq:{model}"
            if self._is_cooled(tag):
                continue
            t0 = time.time()
            try:
                payload: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                }
                if json_mode:
                    payload["response_format"] = {"type": "json_object"}
                data = self._http_json(
                    "https://api.groq.com/openai/v1/chat/completions",
                    payload,
                    {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                )
                text = self._extract_text(data)
                latency = (time.time() - t0) * 1000
                log_event("llm", f"Groq {model}", text[:240], {"provider": "groq", "model": model, "pool": self._pool_name})
                return LLMResponse(text=text, provider="groq", model=model, latency_ms=latency, raw=data)
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    self._cooling(tag, 90.0)
                    continue
                raise
        raise RuntimeError("Groq rate limited")

    def _try_gemini(self, messages: list[dict[str, str]], max_tokens: int) -> LLMResponse:
        key = self._env_keys.get("GEMINI_API_KEY") or self._env_keys.get("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError("no gemini key")
        prompt = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages)
        for model in GEMINI_MODELS:
            tag = f"gemini:{model}"
            if self._is_cooled(tag):
                continue
            t0 = time.time()
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
            try:
                data = self._http_json(
                    url,
                    {
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.4},
                    },
                    {"Content-Type": "application/json"},
                )
                text = self._extract_text(data)
                latency = (time.time() - t0) * 1000
                log_event("llm", f"Gemini {model}", text[:240], {"provider": "gemini", "model": model, "pool": self._pool_name})
                return LLMResponse(text=text, provider="gemini", model=model, latency_ms=latency, raw=data)
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    self._cooling(tag, 90.0)
                    continue
                raise
        raise RuntimeError("Gemini rate limited")

    def _try_nvidia(
        self,
        messages: list[dict[str, str]],
        max_tokens: int,
        *,
        json_mode: bool = False,
    ) -> LLMResponse:
        """NVIDIA NIM API — per-instance model via self._nvidia_model."""
        key = self._env_keys.get("NVIDIA_API_KEY")
        if not key:
            raise RuntimeError("no nvidia_api_key")
        model = self._nvidia_model
        tag = f"nvidia:{model}"
        if self._is_cooled(tag):
            raise RuntimeError(f"nvidia {model} on cooldown")
        t0 = time.time()
        try:
            payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.4,
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            data = self._http_json(
                "https://integrate.api.nvidia.com/v1/chat/completions",
                payload,
                {
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
            )
            text = self._extract_text(data)
            latency = (time.time() - t0) * 1000
            log_event(
                "llm",
                f"NVIDIA {model}",
                text[:240],
                {"provider": "nvidia", "model": model, "pool": self._pool_name},
            )
            return LLMResponse(text=text, provider="nvidia", model=model, latency_ms=latency, raw=data)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 402, 403, 404):
                self._cooling(tag, 120.0 if exc.code == 429 else 45.0)
                raise RuntimeError(f"nvidia rate limited ({exc.code})")
            raise
        except Exception:
            self._cooling(tag, 30.0)
            raise

    def _try_nous(
        self,
        messages: list[dict[str, str]],
        max_tokens: int,
        *,
        json_mode: bool = False,
    ) -> LLMResponse:
        """Nous Portal / StepFun direct inference per agent."""
        key = self._env_keys.get("NOUS_API_KEY")
        if not key:
            raise RuntimeError("no nous api key")
        model = NOUS_STEPFUN_MODEL
        tag = f"nous:{model}:{self._pool_name}"
        if self._is_cooled(tag):
            raise RuntimeError(f"nous {model} on cooldown")
        t0 = time.time()
        try:
            payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.4,
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            url = f"{NOUS_STEPFUN_BASE_URL.rstrip('/')}/v1/chat/completions"
            data = self._http_json(
                url,
                payload,
                {
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
            )
            text = self._extract_text(data)
            latency = (time.time() - t0) * 1000
            tag = f"nous:{model}:{self._pool_name}"
            log_event(
                "llm",
                f"Nous {model}",
                text[:240],
                {"provider": "nous", "model": model, "pool": self._pool_name},
            )
            return LLMResponse(text=text, provider="nous", model=model, latency_ms=latency, raw=data)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 402, 403, 404):
                self._cooling(tag, 120.0 if exc.code == 429 else 45.0)
                raise RuntimeError(f"nous rate limited ({exc.code})")
            raise
        except Exception:
            self._cooling(tag, 30.0)
            raise

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 1500,
        system: str | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        full_messages = messages[:]
        if system:
            full_messages = [{"role": "system", "content": system}] + full_messages

        errors: list[str] = []
        ordered: list[Any] = []
        if "nvidia" in self._provider_priority:
            ordered.append(self._try_nvidia)
        if "openrouter" in self._provider_priority:
            ordered.append(self._try_openrouter)
        if "groq" in self._provider_priority:
            ordered.append(self._try_groq)
        if "gemini" in self._provider_priority:
            ordered.append(self._try_gemini)
        if "nous" in self._provider_priority:
            ordered.append(self._try_nous)

        for fn in ordered:
            try:
                if fn in (self._try_nvidia, self._try_openrouter, self._try_groq, self._try_nous):
                    return fn(full_messages, max_tokens, json_mode=json_mode)
                return fn(full_messages, max_tokens)
            except Exception as exc:
                errors.append(f"{fn.__name__}: {exc}")
        raise RuntimeError("All LLM providers failed: " + "; ".join(errors))

    def chat_race(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 1500,
        system: str | None = None,
        json_mode: bool = False,
        parallel_workers: int = 3,
    ) -> LLMResponse:
        """Race multiple provider routes in parallel — first success wins, no wall-clock cutoff."""
        full_messages = messages[:]
        if system:
            full_messages = [{"role": "system", "content": system}] + full_messages

        routes: list[tuple[str, Any]] = []
        if "nvidia" in self._provider_priority and self._env_keys.get("NVIDIA_API_KEY"):
            routes.append(("nvidia", lambda: self._try_nvidia(full_messages, max_tokens, json_mode=json_mode)))
        if "openrouter" in self._provider_priority and self._or_keys:
            routes.append(("openrouter", lambda: self._try_openrouter(full_messages, max_tokens, json_mode=json_mode)))
        if "groq" in self._provider_priority and self._env_keys.get("GROQ_API_KEY"):
            routes.append(("groq", lambda: self._try_groq(full_messages, max_tokens, json_mode=json_mode)))
        if "gemini" in self._provider_priority and (self._env_keys.get("GEMINI_API_KEY") or self._env_keys.get("GOOGLE_API_KEY")):
            routes.append(("gemini", lambda: self._try_gemini(full_messages, max_tokens)))
        if "nous" in self._provider_priority and self._env_keys.get("NOUS_API_KEY"):
            routes.append(("nous", lambda: self._try_nous(full_messages, max_tokens, json_mode=json_mode)))

        if not routes:
            return self.chat(messages, max_tokens=max_tokens, system=system, json_mode=json_mode)

        workers = max(1, min(parallel_workers, len(routes) * 2))
        errors: list[str] = []

        def _attempt(route_name: str, fn: Any) -> LLMResponse:
            try:
                return fn()
            except Exception as exc:
                raise RuntimeError(f"{route_name}: {exc}") from exc

        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending: set[Any] = set()
            for route_name, fn in routes:
                pending.add(pool.submit(_attempt, route_name, fn))
                pending.add(pool.submit(_attempt, route_name, fn))

            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done:
                    try:
                        return fut.result()
                    except Exception as exc:
                        errors.append(str(exc))
        raise RuntimeError("All parallel LLM routes failed: " + "; ".join(errors[:6]))

    def status(self) -> dict[str, Any]:
        return {
            "nvidia_key": bool(self._env_keys.get("NVIDIA_API_KEY")),
            "openrouter_keys": len(self._or_keys),
            "optional_keys": list(self._env_keys.keys()),
            "fallback_models": FREE_OPENROUTER_MODELS,
            "cooldown_sec": self._min_cooldown_remaining(),
            "openrouter_models": self._openrouter_models,
            "pool": self._pool_name,
            "provider_priority": list(self._provider_priority),
        }

    def _min_cooldown_remaining(self) -> float:
        if not self._cooldown:
            return 0.0
        now = time.time()
        remain = [until - now for until in self._cooldown.values() if until > now]
        return max(remain) if remain else 0.0

    def wait_for_provider(self, max_wait: float | None = None) -> bool:
        """Block until at least one provider route is off cooldown (no deadline by default)."""
        deadline = (time.time() + max_wait) if max_wait is not None else None
        while True:
            if self._min_cooldown_remaining() <= 0:
                return True
            if not (
                self._or_keys
                or self._env_keys.get("GROQ_API_KEY")
                or self._env_keys.get("GEMINI_API_KEY")
                or self._env_keys.get("GOOGLE_API_KEY")
            ):
                return False
            sleep_for = min(2.0, max(self._min_cooldown_remaining(), 0.5))
            if deadline is not None:
                remain = deadline - time.time()
                if remain <= 0:
                    return self._min_cooldown_remaining() <= 0
                sleep_for = min(sleep_for, remain)
            time.sleep(sleep_for)
