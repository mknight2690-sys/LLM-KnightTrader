import json
from blofin.account_cache import _read_activity_tail_lines

keys = (
    "Opened",
    "Open ",
    "Opening",
    "Decision",
    "Cycle error",
    "paper",
    "Paper",
    "Demo",
    "started",
    "skipped",
    "blocked",
    "Startup",
)
for line in _read_activity_tail_lines(3000)[-100:]:
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        continue
    t = str(ev.get("title") or "")
    if ev.get("type") in ("trade", "error", "system", "llm") and any(k in t for k in keys):
        detail = str(ev.get("detail") or "")[:140]
        print(f"{ev.get('ts')} [{ev.get('type')}] {t} | {detail}")
