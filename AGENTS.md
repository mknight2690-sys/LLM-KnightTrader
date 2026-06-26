# AI agents

**Read [ReadMe.tx](ReadMe.tx) first** — full setup runbook.

Before running anything, you **must ask the user** for:

1. **BloFin API credentials** (API Key, Secret Key, Passphrase) — user may **paste in chat** or give a file path → you write `credentials/blofin.txt` (never commit)
2. **LLM API key** (at least `OPENROUTER_API_KEY`) — user may **paste in chat** or give a file path → you write `.env` (never commit)
3. **Country of residence** and, if they use **ProtonVPN**, which **exit country** is connected — must **not** be a [BloFin-restricted jurisdiction](https://blofin.com/en/support/Terms/7296238795279-TERMS-OF-USE)

Private 1:1 agent chat is fine for keys during setup. Never commit secrets or post them in public forums.

ProtonVPN Free: use **Netherlands, Switzerland, Poland, Romania, Norway, or Japan** for BloFin.  
Do **not** use **US, Canada, Singapore, or Mexico** as VPN exit for BloFin.

First successful start creates desktop shortcuts: **Start LLM KnightTrader** and **Stop LLM KnightTrader**.

## Agent finish protocol (mandatory)

1. **Start** stack once (`launcher\Start LLM KnightTrader.bat` or `python scripts\stack_launcher.py start`)
2. **Verify end-to-end:**
   - `GET /api/health` → ok
   - `GET /api/stack/status` → trader `online`, count 1
   - `GET /api/status` → equity/available sane
   - Dashboard loads; desktop shortcuts exist
3. **Stop** stack: `launcher\Stop LLM KnightTrader.bat` or `python scripts\stack_launcher.py stop`
4. Confirm: `python -c "from trader.stack_control import is_entire_stack_stopped; print(is_entire_stack_stopped())"` → `True`
5. Tell user setup is complete — **they** start daily via desktop **Start LLM KnightTrader** only

**Do not** leave trader/dashboard running. **Do not** add startup tasks or background monitors.

Share prompt: **[SHARE_PROMPT.txt](SHARE_PROMPT.txt)**
