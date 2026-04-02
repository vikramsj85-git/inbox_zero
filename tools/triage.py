"""
tools/triage.py — Email triage engine
Batches emails, sends to Claude for classification, applies Gmail labels.
Financial newsletters get full body fetched for rich digest summaries.
"""

import json
from typing import List, Tuple
from models import EmailSummary, TriageResult, PriorityLevel
from tools.coach import call_claude_json
from tools.gmail_client import ensure_labels_exist, apply_labels, fetch_thread
from prompts import TRIAGE_TEMPLATE
from config import settings
from tools.sender_memory import get_sender_hint, update_from_triage

# All labels Inbox Zero will create/use
ALL_LABELS = [
    "ACTION/Urgent",
    "ACTION/Follow-Up",
    "ACTION/To-Read",
    "ACTION/Unsub",
    "ACTION/Archive",
    "JOBS/Opportunities",
    "JOBS/Recruiters",
    "JOBS/Active",
    "FINANCE/Markets",
    "LEARNING/Professional-Coaching",
]

# Senders that get full body fetched for rich financial summarization
FINANCIAL_SENDERS = [
    "dailyshot", "daily shot", "phil rosen", "axios markets", "axios",
    "macro compass", "concoda", "doomberg", "bankless", "the block",
    "messari", "morning brew", "bloomberg", "wsj", "ft.com", "reuters",
    "coindesk", "decrypt", "the defiant", "pomp", "real vision",
    "grant williams", "raoul pal", "lyn alden", "macro institute",
    "barron", "marketwatch", "seeking alpha", "finimize",
    "the daily upside", "chartr", "money stuff", "odd lots",
]


def _is_financial(email: EmailSummary) -> bool:
    """Detect financial newsletter senders."""
    haystack = (email.sender + email.sender_email + email.subject).lower()
    return any(s in haystack for s in FINANCIAL_SENDERS)


def _format_emails_for_prompt(emails: List[EmailSummary], full_bodies: dict = None) -> str:
    """
    Compact text representation of emails for Claude.
    Financial newsletters get full body included for rich summarization.
    full_bodies: {message_id: full_text} for financial emails
    """
    lines = []
    full_bodies = full_bodies or {}
    for i, e in enumerate(emails, 1):
        body = full_bodies.get(e.message_id, e.body_preview[:300])
        is_fin = e.message_id in full_bodies
        hint = get_sender_hint(e.sender_email)
        hint_line = f"  Memory: {hint}\n" if hint else ""
        lines.append(
            f"EMAIL {i}{'  [FINANCIAL NEWSLETTER — produce rich summary in reasoning field]' if is_fin else ''}:\n"
            f"  ID: {e.message_id}\n"
            f"  From: {e.sender} <{e.sender_email}>\n"
            f"  Subject: {e.subject}\n"
            f"  Date: {e.date.strftime('%Y-%m-%d %H:%M')}\n"
            f"{hint_line}"
            f"  Content: {body[:2000]}\n"
        )
    return "\n".join(lines)


def triage_batch(
    emails: List[EmailSummary],
    service=None,
    dry_run: bool = False
) -> Tuple[List[TriageResult], float]:
    """
    Send a batch of emails to Claude for triage.
    Fetches full body for financial newsletters before sending.
    Returns (triage_results, cost_usd).
    """
    if not emails:
        return [], 0.0

    # Fetch full body for financial newsletters
    full_bodies = {}
    if service and not dry_run:
        for e in emails:
            if _is_financial(e):
                try:
                    full_bodies[e.message_id] = fetch_thread(service, e.thread_id)
                    print(f"  📊 Full body fetched: {e.sender} — {e.subject[:40]}")
                except Exception:
                    pass  # Fall back to preview if fetch fails

    prompt = TRIAGE_TEMPLATE.format(
        count=len(emails),
        emails_text=_format_emails_for_prompt(emails, full_bodies)
    )

    # Use Sonnet for batches containing financial content (richer reasoning needed)
    # Use Haiku for pure triage batches (classification only)
    model = settings.llm_digest if full_bodies else settings.llm_triage
    raw_results, cost = call_claude_json(prompt, model=model, dry_run=dry_run)

    # If JSON parse failed and we have multiple emails, split and retry
    if not raw_results and len(emails) > 1:
        print(f"  ↩ JSON parse failed — splitting batch of {len(emails)} and retrying...")
        mid = len(emails) // 2
        r1, c1 = triage_batch(emails[:mid], service=service, dry_run=dry_run)
        r2, c2 = triage_batch(emails[mid:], service=service, dry_run=dry_run)
        return r1 + r2, cost + c1 + c2

    results = []
    for r in raw_results:
        try:
            results.append(TriageResult(
                message_id=r.get("message_id", ""),
                subject=r.get("subject", ""),
                sender_email=r.get("sender_email", ""),
                priority=PriorityLevel(r.get("priority", "low")),
                labels=r.get("labels", []),
                actions=r.get("actions", []),
                reasoning=r.get("reasoning", ""),
                draft_needed=r.get("draft_needed", False),
                unsub_candidate=r.get("unsub_candidate", False),
            ))
        except Exception as e:
            print(f"⚠️  Could not parse triage result: {e} | Raw: {r}")

    return results, cost


def apply_triage_results(
    service,
    results: List[TriageResult],
    label_map: dict,
    dry_run: bool = False
) -> dict:
    """
    Apply Claude's triage decisions to Gmail.
    Labels emails according to triage results.
    Returns summary counts.
    """
    counts = {"urgent": 0, "high": 0, "medium": 0, "low": 0, "unsub": 0, "errors": 0}

    for result in results:
        if dry_run:
            print(f"[DRY RUN] Would label '{result.subject}' → {result.labels} ({result.priority})")
            continue

        label_ids = [label_map[l] for l in result.labels if l in label_map]

        if label_ids:
            success = apply_labels(service, result.message_id, label_ids)
            if success:
                counts[result.priority.value if result.priority.value in counts else "low"] += 1
            else:
                counts["errors"] += 1

    return counts


def run_triage(service, emails: List[EmailSummary], dry_run: bool = False) -> dict:
    """
    Full triage pipeline: classify → label → return summary.
    Processes emails in batches. Financial newsletters get full-body fetch.
    """
    if not emails:
        print("📭 No unread emails to triage.")
        return {"processed": 0, "total_cost": 0.0}

    fin_count = sum(1 for e in emails if _is_financial(e))
    print(f"\n[Inbox Zero] Triaging {len(emails)} emails ({fin_count} financial newsletters)...")

    label_map = {} if dry_run else ensure_labels_exist(service, ALL_LABELS)

    all_results = []
    total_cost = 0.0
    batch_size = settings.triage_batch_size

    for i in range(0, len(emails), batch_size):
        batch = emails[i:i + batch_size]
        print(f"  → Batch {i//batch_size + 1}: {len(batch)} emails...")

        results, cost = triage_batch(batch, service=service, dry_run=dry_run)
        total_cost += cost

        if not dry_run:
            apply_triage_results(service, results, label_map, dry_run=dry_run)

        all_results.extend(results)

    urgent = sum(1 for r in all_results if r.priority == PriorityLevel.URGENT)
    high   = sum(1 for r in all_results if r.priority == PriorityLevel.HIGH)
    drafts = sum(1 for r in all_results if r.draft_needed)
    unsubs = sum(1 for r in all_results if r.unsub_candidate)

    # Update sender memory from this run — zero API cost
    update_from_triage(all_results, emails)

    print(f"\n✅ Triage complete")
    print(f"   Urgent: {urgent} | High: {high} | Drafts needed: {drafts} | Unsub candidates: {unsubs}")
    print(f"   Cost: ${total_cost:.4f}")

    return {
        "processed": len(all_results),
        "results": all_results,
        "urgent_count": urgent,
        "drafts_needed": drafts,
        "unsub_candidates": unsubs,
        "total_cost": total_cost,
    }

