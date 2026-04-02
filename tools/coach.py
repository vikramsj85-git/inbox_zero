"""
tools/coach.py — Claude API interface for Inbox Zero
Prompt caching on system prompt cuts cost ~80% across a triage session.
"""

import json
import anthropic
from config import settings
from prompts import INBOX_ZERO_SYSTEM_V1

client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

COST_PER_1M = {
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00,  "cache_read": 0.08, "cache_write": 1.00},
}


def _calculate_cost(usage, model: str) -> float:
    rates = COST_PER_1M.get(model, COST_PER_1M["claude-sonnet-4-6"])
    return (
        (getattr(usage, "input_tokens", 0) / 1_000_000) * rates["input"] +
        (getattr(usage, "output_tokens", 0) / 1_000_000) * rates["output"] +
        (getattr(usage, "cache_read_input_tokens", 0) / 1_000_000) * rates["cache_read"] +
        (getattr(usage, "cache_creation_input_tokens", 0) / 1_000_000) * rates["cache_write"]
    )


def call_claude(user_prompt: str, model: str, dry_run: bool = False) -> tuple[str, float]:
    """
    Call Claude with cached system prompt.
    Returns (response_text, cost_usd).
    """
    if dry_run:
        return "[DRY RUN] Claude response would appear here.", 0.0

    response = client.messages.create(
        model=model,
        max_tokens=settings.max_tokens,
        system=[{
            "type": "text",
            "text": INBOX_ZERO_SYSTEM_V1,
            "cache_control": {"type": "ephemeral"}  # Cache system prompt
        }],
        messages=[{"role": "user", "content": user_prompt}]
    )

    cost = _calculate_cost(response.usage, model)
    return response.content[0].text, cost


def call_claude_json(user_prompt: str, model: str, dry_run: bool = False) -> tuple[list | dict, float]:
    """
    Call Claude expecting JSON response. Strips markdown fences before parsing.
    Returns (parsed_json, cost_usd).
    """
    text, cost = call_claude(user_prompt, model, dry_run)

    if dry_run:
        return [], 0.0

    # Strip markdown fences if present
    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[1]
    if clean.endswith("```"):
        clean = clean.rsplit("```", 1)[0]

    try:
        return json.loads(clean.strip()), cost
    except json.JSONDecodeError:
        # Strip trailing commas before closing brackets/braces (common Claude mistake)
        import re
        clean = re.sub(r",\s*([}\]])", r"\1", clean.strip())
        try:
            return json.loads(clean), cost
        except json.JSONDecodeError as e:
            print(f"⚠️  JSON parse error: {e}\nRaw response: {text[:200]}")
            return [], cost
