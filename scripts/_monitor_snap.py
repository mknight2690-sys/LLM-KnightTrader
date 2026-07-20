"""Quick stack/trade snapshot for monitoring."""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from blofin.paper_ledger import LEDGER_PATH
from config import STATE_FILE


def get(path: str, timeout: float = 15):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:8765{path}", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as exc:
        return {"_err": str(exc)}


def main() -> None:
    h = get("/api/health", 5)
    st = get("/api/stack/status", 25)
    s = get("/api/status", 25)
    print("health", h)
    if isinstance(st, dict) and "_err" not in st:
        print("trader", st.get("trader"))
        print("dashboard", st.get("dashboard"))
    else:
        print("stack", st)
    acct = (s.get("account") or {}) if isinstance(s, dict) else {}
    state = (s.get("state") or {}) if isinstance(s, dict) else {}
    print(
        "equity",
        acct.get("equity"),
        "avail",
        acct.get("available"),
        "paper",
        s.get("paper_trading") if isinstance(s, dict) else None,
        "pos",
        len(acct.get("positions") or []),
    )
    bp = state.get("best_params") or {}
    print("cycles", state.get("cycles"), "strategy", bp.get("STRATEGY"), "margin", bp.get("MARGIN_USE_RATIO"))
    ld = state.get("last_decision") or {}
    print(
        "last_decision",
        ld.get("action"),
        ld.get("instId"),
        ld.get("side"),
        str(ld.get("reasoning") or "")[:120],
    )
    led = json.loads(LEDGER_PATH.read_text(encoding="utf-8")) if LEDGER_PATH.is_file() else {}
    fills = led.get("fills") or []
    print("ledger_cash", led.get("cash"), "ledger_pos", list((led.get("positions") or {}).keys()), "fills", len(fills))
    disk = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.is_file() else {}
    trades = [t for t in (disk.get("trades") or []) if isinstance(t, dict)]
    opens = [t for t in trades if str(t.get("action") or "").startswith("open") and t.get("ok") is not False]
    print("state_trades", len(trades), "openish", len(opens))
    for t in trades[-10:]:
        print(
            " ",
            t.get("action"),
            t.get("instId"),
            "ok=",
            t.get("ok"),
            str(t.get("reason") or t.get("error") or "")[:100],
        )


if __name__ == "__main__":
    main()
