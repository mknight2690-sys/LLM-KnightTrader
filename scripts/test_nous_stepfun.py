"""Smoke test: confirm LLMWrapper can reach Nous Portal stepfun instance."""

from __future__ import annotations



import sys

from llm.wrapper import LLMWrapper





def main() -> int:

    llm = LLMWrapper(

        provider_priority=("nous",),

        pool_name="local_nous_test",

        nvidia_model="stepfun/step-3.7-flash:free",

    )

    resp = llm.chat(

        messages=[{"role": "user", "content": "Return only the word OK."}],

        system="Return only the word OK.",

        max_tokens=24,

    )

    text = (resp.text or "").strip()

    print(f"provider={resp.provider} model={resp.model} latency_ms={resp.latency_ms:.1f} text={text!r}")

    ok = resp.provider == "nous" and resp.model == "stepfun/step-3.7-flash:free" and "OK" in text.upper()

    return 0 if ok else 2





if __name__ == "__main__":

    sys.exit(main())

