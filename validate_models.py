"""Validate every OpenRouter model used by the trader end-to-end."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from llm.model_registry import FREE_OPENROUTER_MODELS, AGENT_MODEL_MAP
from credentials import discover_openrouter_keys

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
TEST_PROMPT = "Reply with exactly the word OK"
MAX_TOKENS = 16


def http_json(url, payload=None, headers=None, method="POST", timeout=60):
    data = json.dumps(payload).encode("utf-8") if method == "POST" and payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def main():
    keys = discover_openrouter_keys()
    if not keys:
        print("NO_OPENROUTER_KEYS")
        sys.exit(1)

    print(f"OpenRouter keys found: {len(keys)}")

    # 1) Refresh free model catalog
    free_model_ids = set()
    try:
        catalog = http_json(
            OPENROUTER_MODELS_URL,
            method="GET",
            headers={"Authorization": f"Bearer {keys[0]}"},
            timeout=30,
        )
        for m in catalog.get("data", []):
            mid = m.get("id", "")
            pricing = m.get("pricing", {})
            prompt_price = pricing.get("prompt", "1")
            if ":free" in mid or prompt_price in ("0", 0, "0.0"):
                free_model_ids.add(mid)
        print(f"Free models on OpenRouter right now: {len(free_model_ids)}")
    except Exception as exc:
        print(f"WARNING: could not fetch model catalog: {exc}")

    # 2) Determine unique configured models
    configured_pinned = set()
    configured_pinned.update(FREE_OPENROUTER_MODELS)
    configured_pinned.update(AGENT_MODEL_MAP.values())

    print(f"Configured unique models: {len(configured_pinned)}")
    results = {}

    for model in sorted(configured_pinned):
        ok = False
        status = "skipped"
        error = ""
        latency_ms = None
        provider = "openrouter"
        tested_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        if model not in free_model_ids:
            status = "not_in_catalog"
            error = "missing from free catalog"
        else:
            status = "tested"
            # Try keys in round-robin until one works or all fail
            for ki in range(len(keys)):
                key = keys[ki % len(keys)]
                tag = f"or:{key[:12]}:{model}"
                t0 = time.time()
                try:
                    payload = {
                        "model": model,
                        "messages": [
                            {"role": "user", "content": TEST_PROMPT}
                        ],
                        "max_tokens": MAX_TOKENS,
                        "temperature": 0.0,
                    }
                    headers = {
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "http://localhost:8765",
                        "X-Title": "LLM-KnightTrader-E2E",
                    }
                    data = http_json(CHAT_URL, payload=payload, headers=headers, timeout=60)
                    text = ""
                    if "choices" in data:
                        msg = data["choices"][0].get("message") or {}
                        text = (msg.get("content") or "").strip()
                    elif "candidates" in data:
                        parts = data["candidates"][0].get("content", {}).get("parts", [])
                        text = "".join(p.get("text", "") for p in parts).strip()
                    latency_ms = int((time.time() - t0) * 1000)
                    if text:
                        ok = True
                        status = "ok"
                        break
                    else:
                        status = "empty"
                        error = "empty response"
                        break
                except urllib.error.HTTPError as exc:
                    latency_ms = int((time.time() - t0) * 1000)
                    body = ""
                    try:
                        body = exc.read().decode("utf-8", errors="replace")[:200]
                    except Exception:
                        body = str(exc)
                    status = f"http_{exc.code}"
                    error = body.replace("\n", " ")
                    if exc.code in (429, 402, 403, 404):
                        continue
                    break
                except Exception as exc:
                    latency_ms = int((time.time() - t0) * 1000)
                    status = "exception"
                    error = str(exc)[:200]
                    break
            # small sleep between models to be nice to free tier
            time.sleep(0.2)

        results[model] = {
            "status": status,
            "ok": ok,
            "provider": provider,
            "latency_ms": latency_ms,
            "error": error,
            "tested_at": tested_at,
            "in_catalog": model in free_model_ids,
        }
        marker = "OK " if ok else "FAIL"
        print(f"[{marker}] {model}: {status} | {latency_ms} ms | {error}")

    # 3) Recommendation logic
    broken = [m for m, r in results.items() if not r["ok"]]
    good = [m for m, r in results.items() if r["ok"]]

    print("\n=== SUMMARY ===")
    print(f"Good: {len(good)}")
    print(f"Broken: {len(broken)}")
    if broken:
        print("Broken models:", ", ".join(broken))
        print("Action: Update model_registry.py to remove broken models and substitute current free catalog models.")
    else:
        print("All tested models responded OK.")

    out_path = PROJECT_ROOT / "data" / "model_validation.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"results": results, "broken": broken, "good": good}, indent=2), encoding="utf-8")
    print(f"\nSaved detailed results to {out_path}")


if __name__ == "__main__":
    main()
