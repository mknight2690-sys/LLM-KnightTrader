# AI agents

**Read [ReadMe.tx](ReadMe.tx) first** — full setup runbook.

Before running anything, you **must ask the user** for:

1. **BloFin API credentials** (API Key, Secret Key, Passphrase) → `credentials/blofin.txt` (never commit)
2. **LLM API key** (at least `OPENROUTER_API_KEY`) → `.env` (never commit)
3. **Country of residence** and, if they use **ProtonVPN**, which **exit country** is connected — must **not** be a [BloFin-restricted jurisdiction](https://blofin.com/en/support/Terms/7296238795279-TERMS-OF-USE)

ProtonVPN Free: use **Netherlands, Switzerland, Poland, Romania, Norway, or Japan** for BloFin.  
Do **not** use **US, Canada, Singapore, or Mexico** as VPN exit for BloFin.

First successful start creates desktop shortcuts: **Start LLM KnightTrader** and **Stop LLM KnightTrader**.

**Daily operation:** user double-clicks **Start** (cold restart: stop all → dashboard + trader) or **Stop** (kill entire stack). After setup verify, run Stop — do not leave stack running in background.

Share prompt for others: **SHARE_PROMPT.txt** in repo root.