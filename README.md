# LLM KnightTrader

Autonomous **BloFin USDT perpetual futures** bot with a live dashboard, multi-provider LLM decisions, auto-harvest, and self-repair.

**AI agents:** read **[ReadMe.tx](ReadMe.tx)** first — full setup, architecture, API, and troubleshooting.  
Also see **[AGENTS.md](AGENTS.md)** for required user questions (BloFin creds, LLM keys, ProtonVPN/geo).

## Quick start (Windows)

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy credentials\blofin.example.txt credentials\blofin.txt
copy .env.example .env
REM Edit credentials\blofin.txt and .env with your keys
launcher\Start LLM KnightTrader.bat
```

Creates **Start** / **Stop** desktop shortcuts on first successful start.

Dashboard: **http://127.0.0.1:8765**

## Requirements

- Python **3.12+**
- BloFin API credentials
- At least one LLM key (OpenRouter recommended — free models supported)

## License

MIT — see [LICENSE](LICENSE).
