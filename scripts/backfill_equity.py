"""One-time backfill: extract equity snapshots from activity log."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(r"C:\Users\mknig\hermes-llm-trader")
ACTIVITY_LOG = ROOT / "data" / "activity.jsonl"
EQUITY_HISTORY_FILE = ROOT / "data" / "equity_history.jsonl"

PARSERS = [
    re.compile(r"Equity\s+[$]?([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE),
    re.compile(r"equity[:\s]+[$]?([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE),
]

def parse_equity(title: str) -> float | None:
    for p in PARSERS:
        m = p.search(title)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None

def main() -> None:
    if not ACTIVITY_LOG.is_file():
        print("No activity log found.")
        return

    print(f"Reading {ACTIVITY_LOG}...")
    rows: list[dict] = []
    count = 0
    with ACTIVITY_LOG.open("r", encoding="utf-8") as f:
        for line in f:
            count += 1
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "account":
                continue
            equity = parse_equity(str(ev.get("title", "")))
            if equity and equity > 0:
                rows.append({"at": int(float(ev.get("ts", 0)) * 1000), "equity": equity})

    # Deduplicate by timestamp
    seen: dict[int, float] = {}
    for row in rows:
        seen[row["at"]] = row["equity"]
    unique = [{"at": at, "equity": eq} for at, eq in sorted(seen.items())]

    print(f"Scanned {count} lines. Found {len(unique)} unique equity snapshots.")

    EQUITY_HISTORY_FILE.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in unique),
        encoding="utf-8",
    )
    print(f"Wrote {len(unique)} entries to {EQUITY_HISTORY_FILE}")

if __name__ == "__main__":
    main()
