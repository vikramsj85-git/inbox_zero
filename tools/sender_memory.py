"""
tools/sender_memory.py — Sender pattern learning engine
Builds a local database of sender behavior over time.
Zero API cost — pure pattern matching from triage history.
Gets smarter every run without any extra Claude calls.
"""

import json
from datetime import datetime, date
from pathlib import Path
from collections import defaultdict
from typing import Optional
from config import DATA_DIR

MEMORY_FILE = DATA_DIR / "sender_memory.json"


# ─── Schema ──────────────────────────────────────────────────────────────────
# {
#   "sender@email.com": {
#     "name": "Display Name",
#     "first_seen": "2026-01-01",
#     "last_seen": "2026-02-27",
#     "email_count": 42,
#     "priority_history": ["high", "medium", "medium", "low"],
#     "avg_priority": "medium",
#     "labels_applied": ["FINANCE/Markets", "ACTION/To-Read"],
#     "unsub_flagged": 0,        # times flagged as unsub candidate
#     "draft_requested": 0,      # times a draft reply was needed
#     "overrides": [],           # manual priority overrides by user
#     "notes": ""                # free text, set manually
#   }
# }


def load_memory() -> dict:
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_memory(memory: dict) -> None:
    MEMORY_FILE.write_text(json.dumps(memory, indent=2))


def update_from_triage(triage_results: list, emails: list) -> dict:
    """
    Update sender memory from a completed triage run.
    Called automatically after every triage batch.
    Returns updated memory dict.
    """
    memory = load_memory()
    email_map = {e.message_id: e for e in emails}
    today = date.today().isoformat()

    for result in triage_results:
        email = email_map.get(result.message_id)
        if not email:
            continue

        sender_key = email.sender_email.lower().strip()

        if sender_key not in memory:
            memory[sender_key] = {
                "name": email.sender,
                "first_seen": today,
                "last_seen": today,
                "email_count": 0,
                "priority_history": [],
                "avg_priority": result.priority.value,
                "labels_applied": [],
                "unsub_flagged": 0,
                "draft_requested": 0,
                "overrides": [],
                "notes": "",
            }

        m = memory[sender_key]
        m["last_seen"] = today
        m["email_count"] += 1
        m["name"] = email.sender  # Update display name

        # Track priority history (keep last 20)
        m["priority_history"].append(result.priority.value)
        if len(m["priority_history"]) > 20:
            m["priority_history"] = m["priority_history"][-20:]

        # Recalculate average priority
        m["avg_priority"] = _calc_avg_priority(m["priority_history"])

        # Track labels
        for label in result.labels:
            if label not in m["labels_applied"]:
                m["labels_applied"].append(label)

        # Track unsub flags
        if result.unsub_candidate:
            m["unsub_flagged"] += 1

        # Track draft requests
        if result.draft_needed:
            m["draft_requested"] += 1

    save_memory(memory)
    return memory


def get_sender_hint(sender_email: str) -> Optional[str]:
    """
    Look up a sender in memory and return a priority hint for Claude.
    Returns None if sender is unknown (let Claude decide fresh).
    Called during triage prompt construction.
    """
    memory = load_memory()
    key = sender_email.lower().strip()

    if key not in memory:
        return None

    m = memory[key]

    # Strong unsub signal — flagged 3+ times
    if m["unsub_flagged"] >= 3:
        return f"MEMORY: {sender_email} has been flagged as unsub candidate {m['unsub_flagged']} times. Strongly consider unsubscribe_candidate priority."

    # Check for manual override
    if m["overrides"]:
        latest = m["overrides"][-1]
        return f"MEMORY: User has manually set this sender to {latest['priority']} priority. Use {latest['priority']} unless content clearly warrants otherwise."

    # Established high-priority sender
    if m["avg_priority"] in ["urgent", "high"] and m["email_count"] >= 3:
        return f"MEMORY: {m['name']} is a known high-priority sender ({m['email_count']} emails, avg: {m['avg_priority']}). Bias toward high priority."

    # Established low-signal sender
    if m["avg_priority"] == "low" and m["email_count"] >= 5:
        return f"MEMORY: {m['name']} consistently low priority ({m['email_count']} emails). Bias toward low or unsubscribe_candidate."

    # Draft-heavy sender (professional contact who expects replies)
    if m["draft_requested"] >= 2:
        return f"MEMORY: {m['name']} frequently requires draft replies ({m['draft_requested']} times). Flag draft_needed=true if reply seems warranted."

    return None


def set_sender_override(sender_email: str, priority: str, note: str = "") -> bool:
    """
    Manually override a sender's priority. Persists across all future runs.
    Called by user via: python main.py --override sender@email.com high
    """
    memory = load_memory()
    key = sender_email.lower().strip()

    if key not in memory:
        print(f"⚠️  Sender {sender_email} not in memory yet. Run triage first.")
        return False

    memory[key]["overrides"].append({
        "priority": priority,
        "date": date.today().isoformat(),
        "note": note,
    })
    memory[key]["notes"] = note
    save_memory(memory)
    print(f"✅ Override set: {sender_email} → {priority}")
    return True


def get_memory_stats() -> dict:
    """Summary stats for --memory-report command."""
    memory = load_memory()
    if not memory:
        return {"total_senders": 0}

    priority_counts = defaultdict(int)
    top_senders = []

    for email, m in memory.items():
        priority_counts[m["avg_priority"]] += 1
        top_senders.append((email, m["email_count"], m["avg_priority"], m["name"]))

    top_senders.sort(key=lambda x: x[1], reverse=True)

    return {
        "total_senders": len(memory),
        "priority_breakdown": dict(priority_counts),
        "top_senders": top_senders[:10],
        "unsub_candidates": [
            (e, m["name"], m["unsub_flagged"])
            for e, m in memory.items()
            if m["unsub_flagged"] >= 2
        ],
        "overridden_senders": [
            (e, m["name"], m["overrides"][-1]["priority"])
            for e, m in memory.items()
            if m["overrides"]
        ],
    }


def suggest_new_labels() -> list:
    """
    Analyze sender memory and suggest new labels that might be useful.
    Called weekly — looks for clusters of senders in the same category
    that don't have a good home in the current label structure.
    Zero API cost — pure frequency analysis.
    """
    memory = load_memory()
    suggestions = []

    # Find senders with 5+ emails and only generic labels
    generic_labels = {"ACTION/To-Read", "ACTION/Archive", "ACTION/Unsub"}
    orphaned = []

    for email, m in memory.items():
        if m["email_count"] >= 5:
            specific_labels = set(m["labels_applied"]) - generic_labels
            if not specific_labels and m["avg_priority"] in ["medium", "high"]:
                orphaned.append((email, m["name"], m["email_count"]))

    if len(orphaned) >= 3:
        suggestions.append({
            "type": "new_label",
            "reason": f"{len(orphaned)} frequent senders have no specific label",
            "senders": orphaned[:5],
            "suggestion": "Review these senders and consider creating a custom label",
        })

    return suggestions


def _calc_avg_priority(history: list) -> str:
    """Convert priority history list to a single average priority string."""
    if not history:
        return "medium"

    weights = {"urgent": 4, "high": 3, "medium": 2, "low": 1, "unsubscribe_candidate": 0}
    reverse = {4: "urgent", 3: "high", 2: "medium", 1: "low", 0: "unsubscribe_candidate"}

    # Weight recent history more heavily (last 5 get 2x weight)
    recent = history[-5:]
    older = history[:-5]

    total = sum(weights.get(p, 2) for p in older)
    total += sum(weights.get(p, 2) * 2 for p in recent)
    count = len(older) + len(recent) * 2

    avg = round(total / count) if count else 2
    avg = max(0, min(4, avg))
    return reverse[avg]
