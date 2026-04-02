"""
config.py — Inbox Zero configuration
"""

import os
from pathlib import Path
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

load_dotenv(Path.home() / ".env")

# ─── Paths ───────────────────────────────────────────────────────────────────
AGENT_ROOT   = Path(__file__).parent
AUTH_DIR     = AGENT_ROOT / "auth"
DATA_DIR     = AGENT_ROOT / "data"
REPORTS_DIR  = AGENT_ROOT / "reports"
LOGS_DIR     = Path.home() / "Documents" / "AI Agents" / "logs"

for d in [AUTH_DIR, DATA_DIR, REPORTS_DIR, LOGS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

CREDENTIALS_FILE = AUTH_DIR / "credentials.json"   # OAuth client credentials
TOKEN_FILE       = AUTH_DIR / "token.json"          # Auto-generated after first auth
PROFILE_FILE     = DATA_DIR / "profile.json"
DIGEST_LOG_FILE  = DATA_DIR / "digest_log.json"

# Gmail OAuth scopes — read + modify (no send — drafts only)
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",    # label, archive, draft
    "https://www.googleapis.com/auth/gmail.send",      # send digest emails to self
]


class InboxZeroSettings(BaseSettings):
    model_config = {"protected_namespaces": ("settings_",), "extra": "ignore"}

    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")

    # Model tiering
    llm_triage: str   = "claude-haiku-4-5-20251001"   # Classification task — Haiku
    llm_draft: str    = "claude-sonnet-4-6"            # Drafting needs quality
    llm_digest: str   = "claude-sonnet-4-6"            # Synthesis task
    llm_unsub: str    = "claude-haiku-4-5-20251001"    # Classification — Haiku

    max_tokens: int = 4096

    # Triage batch size — how many emails to send Claude at once
    # Larger = fewer API calls but more tokens per call
    triage_batch_size: int = 10

    # How many emails to fetch per run
    fetch_limit: int = 50

    # Cost guardrail
    daily_cost_alert_usd: float = 0.50


settings = InboxZeroSettings()
