"""LLM KnightTrader configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

APP_NAME = "LLM KnightTrader"
CHAT_AGENT_NAME = "LLM KnightTrader"

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
ACTIVITY_LOG = DATA_DIR / "activity.jsonl"
STATE_FILE = DATA_DIR / "state.json"
PID_DIR = DATA_DIR / "pids"

BLOFIN_LIVE_BASE = "https://openapi.blofin.com"
BLOFIN_DEMO_BASE = "https://demo-trading-openapi.blofin.com"


def _load_dotenv() -> None:
    """Load PROJECT_ROOT/.env into os.environ (no extra dependency)."""
    path = PROJECT_ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _env_or(key: str, fallback: str) -> str:
    val = os.environ.get(key)
    return val if val is not None else fallback


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "demo")


_load_dotenv()

DEFAULT_CREDENTIALS_PATH = Path(
    os.environ.get(
        "BLOFIN_CREDENTIALS_PATH",
        str(PROJECT_ROOT / "credentials" / "blofin.txt"),
    )
)

# Demo trading: set BLOFIN_DEMO=1 (or true/yes/on/demo). Explicit BLOFIN_API_BASE wins.
BLOFIN_DEMO = _env_bool("BLOFIN_DEMO", default=False)
_api_base_env = os.environ.get("BLOFIN_API_BASE") or os.environ.get("BLOFIN_BASE_URL")
if _api_base_env:
    BLOFIN_BASE = _api_base_env.strip().rstrip("/")
    BLOFIN_DEMO = "demo-trading" in BLOFIN_BASE.lower() or BLOFIN_DEMO
elif BLOFIN_DEMO:
    BLOFIN_BASE = BLOFIN_DEMO_BASE
else:
    BLOFIN_BASE = BLOFIN_LIVE_BASE

BLOFIN_BROKER_ID = os.environ.get("BLOFIN_BROKER_ID", "5388cb1f51cec2e3")

DASHBOARD_HOST = os.environ.get(
    "KNIGHTTRADER_DASHBOARD_HOST",
    os.environ.get("HERMES_DASHBOARD_HOST", "127.0.0.1"),
)
DASHBOARD_PORT = int(
    os.environ.get(
        "KNIGHTTRADER_DASHBOARD_PORT",
        os.environ.get("HERMES_DASHBOARD_PORT", "8765"),
    )
)

TARGET_EQUITY = float(
    os.environ.get(
        "KNIGHTTRADER_TARGET_EQUITY",
        os.environ.get("HERMES_TARGET_EQUITY", "1000000"),
    )
)
TRADER_LOOP_SEC = float(
    os.environ.get(
        "KNIGHTTRADER_TRADER_LOOP_SEC",
        os.environ.get("HERMES_TRADER_LOOP_SEC", "60"),
    )
)

# How often any process may refresh balance/positions from BloFin (seconds).
ACCOUNT_REFRESH_SEC = float(
    os.environ.get(
        "KNIGHTTRADER_ACCOUNT_REFRESH_SEC",
        os.environ.get("HERMES_ACCOUNT_REFRESH_SEC", "15"),
    )
)

# Startup/smoke-test HTTP timeout on direct API probes (seconds).
LLM_HTTP_TIMEOUT_SEC = float(os.environ.get("KNIGHTTRADER_LLM_HTTP_TIMEOUT_SEC", "600"))

# Game mode: raise leverage as needed so min contract margin fits available balance.
TRADE_MODE = os.environ.get("KNIGHTTRADER_TRADE_MODE", "game").strip().lower()
_LEV_ENV = os.environ.get("KNIGHTTRADER_MAX_LEVERAGE")
try:
    TRADE_MAX_LEVERAGE = int(_LEV_ENV) if _LEV_ENV is not None else 20
except (TypeError, ValueError):
    TRADE_MAX_LEVERAGE = 20
_MARGIN_ENV = os.environ.get("KNIGHTTRADER_MARGIN_USE_RATIO")
try:
    MARGIN_USE_RATIO = float(_MARGIN_ENV) if _MARGIN_ENV is not None else 0.35
except (TypeError, ValueError):
    MARGIN_USE_RATIO = 0.35
LEVERAGE_LADDER = [
    int(x)
    for x in os.environ.get("KNIGHTTRADER_LEVERAGE_LADDER", "3,5,10,15,20").split(",")
    if x.strip().isdigit()
] or [3, 5, 10, 15, 20]

# Harvest winners at +NTP%.
_HARVEST_ENV = os.environ.get("KNIGHTTRADER_HARVEST_NTP_PCT")
try:
    BLOHUNTER_HARVEST_NTP_PCT = float(_HARVEST_ENV) if _HARVEST_ENV is not None else 10.0
except (TypeError, ValueError):
    BLOHUNTER_HARVEST_NTP_PCT = 10.0

_OPEN_ENV = os.environ.get("KNIGHTTRADER_OPEN_CONFIDENCE_FLOOR")
try:
    OPEN_CONFIDENCE_FLOOR = float(_OPEN_ENV) if _OPEN_ENV is not None else 60.0
except (TypeError, ValueError):
    OPEN_CONFIDENCE_FLOOR = 60.0
_FALLBACK_ENV = os.environ.get("KNIGHTTRADER_FALLBACK_OPEN_CONFIDENCE_FLOOR")
try:
    FALLBACK_OPEN_CONFIDENCE_FLOOR = (
        float(_FALLBACK_ENV) if _FALLBACK_ENV is not None else 55.0
    )
except (TypeError, ValueError):
    FALLBACK_OPEN_CONFIDENCE_FLOOR = 55.0

_MAX_POS_ENV = os.environ.get("KNIGHTTRADER_MAX_POSITIONS")
try:
    MAX_POSITIONS = int(_MAX_POS_ENV) if _MAX_POS_ENV is not None else 4
except (TypeError, ValueError):
    MAX_POSITIONS = 4
_MAX_EXP_ENV = os.environ.get("KNIGHTTRADER_MAX_EXPOSURE_PCT")
try:
    MAX_EXPOSURE_PCT = float(_MAX_EXP_ENV) if _MAX_EXP_ENV is not None else 0.45
except (TypeError, ValueError):
    MAX_EXPOSURE_PCT = 0.45
_TRAIL_ENV = os.environ.get("KNIGHTTRADER_TRAILING_SL_PCT")
try:
    TRAILING_SL_PCT = float(_TRAIL_ENV) if _TRAIL_ENV is not None else 0.012
except (TypeError, ValueError):
    TRAILING_SL_PCT = 0.012
_BE_ENV = os.environ.get("KNIGHTTRADER_BREAKEVEN_SL_PCT")
try:
    BREAKEVEN_SL_PCT = float(_BE_ENV) if _BE_ENV is not None else 0.008
except (TypeError, ValueError):
    BREAKEVEN_SL_PCT = 0.008

# Soft equity baseline for small-account risk (demo $40 test).
_TEST_EQ_ENV = os.environ.get("KNIGHTTRADER_TEST_EQUITY")
try:
    TEST_ACCOUNT_EQUITY = float(_TEST_EQ_ENV) if _TEST_EQ_ENV is not None else 40.0
except (TypeError, ValueError):
    TEST_ACCOUNT_EQUITY = 40.0


def apply_best_params(params: dict[str, Any]) -> None:
    """Override runtime config with best available backtest params when present."""
    global TRADE_MAX_LEVERAGE, MARGIN_USE_RATIO, BLOHUNTER_HARVEST_NTP_PCT
    global OPEN_CONFIDENCE_FLOOR, FALLBACK_OPEN_CONFIDENCE_FLOOR
    global MAX_POSITIONS, MAX_EXPOSURE_PCT, TRAILING_SL_PCT, BREAKEVEN_SL_PCT
    global TRADER_LOOP_SEC

    if not isinstance(params, dict):
        return
    if "TRADE_MAX_LEVERAGE" in params:
        try:
            TRADE_MAX_LEVERAGE = int(params["TRADE_MAX_LEVERAGE"])
        except (TypeError, ValueError):
            pass
    if "MARGIN_USE_RATIO" in params:
        try:
            MARGIN_USE_RATIO = float(params["MARGIN_USE_RATIO"])
        except (TypeError, ValueError):
            pass
    harvest = params.get("HARVEST_NTP_PCT", params.get("BLOHUNTER_HARVEST_NTP_PCT"))
    if harvest is not None:
        try:
            BLOHUNTER_HARVEST_NTP_PCT = float(harvest)
        except (TypeError, ValueError):
            pass
    if "OPEN_CONFIDENCE_FLOOR" in params:
        try:
            OPEN_CONFIDENCE_FLOOR = float(params["OPEN_CONFIDENCE_FLOOR"])
        except (TypeError, ValueError):
            pass
    if "FALLBACK_OPEN_CONFIDENCE_FLOOR" in params:
        try:
            FALLBACK_OPEN_CONFIDENCE_FLOOR = float(params["FALLBACK_OPEN_CONFIDENCE_FLOOR"])
        except (TypeError, ValueError):
            pass
    if "MAX_POSITIONS" in params:
        try:
            MAX_POSITIONS = int(params["MAX_POSITIONS"])
        except (TypeError, ValueError):
            pass
    if "MAX_EXPOSURE_PCT" in params:
        try:
            MAX_EXPOSURE_PCT = float(params["MAX_EXPOSURE_PCT"])
        except (TypeError, ValueError):
            pass
    if "TRAILING_SL_PCT" in params:
        try:
            TRAILING_SL_PCT = float(params["TRAILING_SL_PCT"])
        except (TypeError, ValueError):
            pass
    if "BREAKEVEN_SL_PCT" in params:
        try:
            BREAKEVEN_SL_PCT = float(params["BREAKEVEN_SL_PCT"])
        except (TypeError, ValueError):
            pass
    if "TRADER_LOOP_SEC" in params:
        try:
            TRADER_LOOP_SEC = float(params["TRADER_LOOP_SEC"])
        except (TypeError, ValueError):
            pass


# Parallel repair LLM workers — race until one succeeds.
REPAIR_LLM_PARALLEL = max(1, int(os.environ.get("KNIGHTTRADER_REPAIR_LLM_PARALLEL", "3")))
# Nous Portal / StepFun provider routing.
NOUS_STEPFUN_MODEL = os.environ.get("KNIGHTTRADER_NOUS_STEPFUN_MODEL", "stepfun/step-3.7-flash:free")
NOUS_STEPFUN_BASE_URL = os.environ.get("KNIGHTTRADER_NOUS_BASE_URL", "https://inference-api.nousresearch.com")
NOUS_STEPFUN_KEY_PATH = os.environ.get(
    "KNIGHTTRADER_NOUS_KEY_PATH",
    str(Path.home() / "OneDrive" / "Documents" / "Nous API Key Stepfun.txt"),
)

MISSION_PROMPT = (
    "research how to grow crypto futures account the fastest to get a baseline, "
    "learn as you go, and trade the configured BloFin account to 1 million "
    "as fast as you can. make no mistakes"
)
