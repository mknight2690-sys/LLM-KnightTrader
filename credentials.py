"""Load BloFin and LLM API credentials."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from config import PROJECT_ROOT


@dataclass(frozen=True)
class BlofinCredentials:
    api_key: str
    secret_key: str
    passphrase: str


def _parse_key_value_file(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip().lower().replace(" ", "_")] = value.strip()
    return fields


def resolve_blofin_credentials_path() -> Path | None:
    """Find BloFin credential file — env, repo default, then known operator paths."""
    candidates: list[Path] = []
    env_path = os.environ.get("BLOFIN_CREDENTIALS_PATH", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend(
        [
            PROJECT_ROOT / "credentials" / "blofin.txt",
            Path.home() / "Downloads" / "MK Blo Hermes API compendium.txt",
        ]
    )
    downloads = Path.home() / "Downloads"
    if downloads.is_dir():
        for path in sorted(
            downloads.glob("*compendium*.txt"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            candidates.append(path)

    seen: set[str] = set()
    for path in candidates:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            return path
    return None


def load_blofin_credentials(path: Path | None = None) -> BlofinCredentials:
    cred_path = Path(path) if path else resolve_blofin_credentials_path()
    if cred_path is None or not cred_path.is_file():
        raise FileNotFoundError(
            "Blofin credentials not found. Set BLOFIN_CREDENTIALS_PATH, create "
            "credentials/blofin.txt, or place keys in Downloads compendium file."
        )
    fields = _parse_key_value_file(cred_path.read_text(encoding="utf-8"))
    api_key = fields.get("api_key") or fields.get("apikey")
    secret_key = fields.get("secret_key") or fields.get("secretkey")
    passphrase = fields.get("passphrase")
    if not api_key or not secret_key or not passphrase:
        raise ValueError("Credentials file must contain Passphrase, API Key, and Secret Key")
    return BlofinCredentials(api_key=api_key, secret_key=secret_key, passphrase=passphrase)


def discover_openrouter_keys() -> list[str]:
    """Collect OpenRouter keys from env and known credential files."""
    keys: list[str] = []
    seen: set[str] = set()

    def add(key: str | None) -> None:
        if key and key.startswith("sk-or-") and key not in seen:
            seen.add(key)
            keys.append(key)

    add(os.environ.get("OPENROUTER_API_KEY"))

    candidate_files = [
        PROJECT_ROOT / ".env",
        PROJECT_ROOT / "credentials" / "blofin.txt",
        Path.home() / ".fcc" / ".env",
        Path.home() / "AppData" / "Local" / "hermes" / ".env",
    ]
    for path in candidate_files:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r"sk-or-[A-Za-z0-9_-]+", text):
            add(match.group(0))

    return keys


def discover_llm_env_keys() -> dict[str, str]:
    """First key per optional provider (Groq, Gemini)."""
    out: dict[str, str] = {}
    or_keys = discover_openrouter_keys()
    if or_keys:
        out["OPENROUTER_API_KEY"] = or_keys[0]
    for name in ("GROQ_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "NVIDIA_API_KEY"):
        val = os.environ.get(name, "").strip()
        if val:
            out[name] = val
        for path in (Path.home() / "AppData" / "Local" / "hermes" / ".env", Path.home() / ".fcc" / ".env"):
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip().startswith(f"{name}="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        out[name] = val
    # Also check Documents for NVIDIA API Key file (same as MK AI Trader)
    if "NVIDIA_API_KEY" not in out:
        nvidia_file = Path.home() / "OneDrive" / "Documents" / "Nvidia API Key.txt"
        if nvidia_file.is_file():
            try:
                key = nvidia_file.read_text(encoding="utf-8").strip()
                if key:
                    out["NVIDIA_API_KEY"] = key
            except Exception:
                pass
    return out
