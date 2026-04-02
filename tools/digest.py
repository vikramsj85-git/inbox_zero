"""
tools/digest.py — Daily digest and unsubscribe analysis
Generates executive-style daily email summary.
Identifies unsubscribe candidates for user confirmation.
"""

import json
from datetime import datetime
from collections import defaultdict
from typing import List
from models import EmailSummary, TriageResult, PriorityLevel, DigestEntry, DailyDigest
from tools.coach import call_claude, call_claude_json
from prompts import DAILY_DIGEST_TEMPLATE, UNSUB_ANALYSIS_TEMPLATE
from config import settings, DIGEST_LOG_FILE


def generate_daily_digest(
    emails: List[EmailSummary],
    triage_results: List[TriageResult],
    dry_run: bool = False
) -> DailyDigest:
    """
    Generate an executive-style daily digest from triage results.
    """
    # Bucket results by priority
    urgent_items, job_items, read_items, unsub_items = [], [], [], []
    handled_count = 0

    result_map = {r.message_id: r for r in triage_results}
    email_map  = {e.message_id: e for e in emails}

    for result in triage_results:
        email = email_map.get(result.message_id)
        if not email:
            continue

        entry = DigestEntry(
            subject=result.subject,
            sender=email.sender,
            priority=result.priority,
            one_line_summary=result.reasoning,
            action_needed="Reply needed" if result.draft_needed else None,
        )

        if result.priority == PriorityLevel.URGENT:
            urgent_items.append(entry)
        elif result.priority == PriorityLevel.HIGH and "JOB-OPPS" in " ".join(result.labels):
            job_items.append(entry)
        elif result.priority in [PriorityLevel.MEDIUM, PriorityLevel.HIGH]:
            read_items.append(entry)
        elif result.unsub_candidate:
            unsub_items.append(entry)
        else:
            handled_count += 1

    def format_entries(entries: List[DigestEntry]) -> str:
        if not entries:
            return "None"
        return "\n".join(
            f"- {e.subject} (from {e.sender})"
            + (f" — {e.action_needed}" if e.action_needed else "")
            for e in entries
        )

    prompt = DAILY_DIGEST_TEMPLATE.format(
        date=datetime.now().strftime("%A, %B %d %Y"),
        total=len(triage_results),
        urgent_items=format_entries(urgent_items),
        job_items=format_entries(job_items),
        read_items=format_entries(read_items),
        unsub_items=format_entries(unsub_items),
        handled_count=handled_count,
    )

    digest_text, cost = call_claude(prompt, model=settings.llm_digest, dry_run=dry_run)
    print(f"[Inbox Zero] Digest generated | Cost: ${cost:.4f}")

    digest = DailyDigest(
        date=datetime.now(),
        total_processed=len(triage_results),
        urgent_count=len(urgent_items),
        action_required=urgent_items,
        job_opportunities=job_items,
        to_read=read_items,
        unsub_candidates=unsub_items,
        already_handled=handled_count,
        digest_text=digest_text,
    )

    # Log digest to file
    if not dry_run:
        _log_digest(digest)
        # Deliver digest to Gmail inbox under DIGESTS/ label
        try:
            from tools.gmail_client import deliver_digest_to_inbox, _digest_to_html
            label_name = f"DIGESTS/{mode.title()}"
            date_str = datetime.now().strftime("%A, %B %d %Y")
            subject = f"[{mode.title()} Digest] {date_str}"
            html = _digest_to_html(digest.digest_text, mode, date_str)
            # service is not available here — delivered from main.py
            digest._pending_gmail_delivery = {
                "subject": subject,
                "html": html,
                "label": label_name,
            }
        except Exception as e:
            print(f"⚠️  Could not prepare Gmail delivery: {e}")

    return digest


def analyze_unsubscribe_candidates(
    emails: List[EmailSummary],
    triage_results: List[TriageResult],
    dry_run: bool = False
) -> list:
    """
    Identify senders to unsubscribe from or bulk delete.
    Returns list for user confirmation — NEVER acts autonomously.
    """
    # Group unsubscribe candidates by sender
    sender_groups = defaultdict(list)
    for result in triage_results:
        if result.unsub_candidate:
            email = next((e for e in emails if e.message_id == result.message_id), None)
            if email:
                sender_groups[email.sender_email].append(email)

    if not sender_groups:
        print("✅ No unsubscribe candidates found.")
        return []

    senders_text = "\n".join(
        f"- {sender} ({len(msgs)} emails): typical subjects like '{msgs[0].subject}'"
        for sender, msgs in sender_groups.items()
    )

    raw_results, cost = call_claude_json(
        UNSUB_ANALYSIS_TEMPLATE.format(senders_text=senders_text),
        model=settings.llm_unsub,
        dry_run=dry_run,
    )

    print(f"[Inbox Zero] Unsubscribe analysis | Cost: ${cost:.4f}")

    # Attach message IDs to results for later bulk delete
    for r in raw_results:
        sender_email = r.get("sender_email", "")
        if sender_email in sender_groups:
            r["message_ids"] = [e.message_id for e in sender_groups[sender_email]]

    return raw_results


def _log_digest(digest: DailyDigest) -> None:
    """Append digest summary to digest_log.json for history."""
    log = []
    if DIGEST_LOG_FILE.exists():
        try:
            log = json.loads(DIGEST_LOG_FILE.read_text())
        except Exception:
            log = []

    log.append({
        "date": digest.date.isoformat(),
        "total_processed": digest.total_processed,
        "urgent_count": digest.urgent_count,
        "job_opportunities": len(digest.job_opportunities),
    })

    DIGEST_LOG_FILE.write_text(json.dumps(log[-30:], indent=2))  # Keep last 30 days


def generate_timed_digest(
    emails: list,
    triage_results: list,
    mode: str = "morning",
    dry_run: bool = False,
    service=None,
) -> "DailyDigest":
    """
    Generate morning or evening digest with mode-appropriate framing.
    mode: "morning" | "evening"
    """
    from prompts import MORNING_DIGEST_TEMPLATE, EVENING_DIGEST_TEMPLATE
    from datetime import datetime

    urgent_items, job_items, read_items, financial_items = [], [], [], []
    handled_count = 0

    email_map = {e.message_id: e for e in emails}

    for result in triage_results:
        email = email_map.get(result.message_id)
        if not email:
            continue

        entry = DigestEntry(
            subject=result.subject,
            sender=email.sender,
            priority=result.priority,
            one_line_summary=result.reasoning,
            action_needed="Reply needed" if result.draft_needed else None,
        )

        # Financial content goes to its own section
        from tools.triage import _is_financial
        if _is_financial(email):
            financial_items.append(entry)
        elif result.priority == PriorityLevel.URGENT:
            urgent_items.append(entry)
        elif result.priority == PriorityLevel.HIGH:
            job_items.append(entry) if "JOB" in " ".join(result.labels) else read_items.append(entry)
        elif result.priority == PriorityLevel.MEDIUM:
            read_items.append(entry)
        else:
            handled_count += 1

    def fmt(entries):
        if not entries:
            return "None"
        return "\n".join(
            f"- {e.subject} (from {e.sender}): {e.one_line_summary[:120]}"
            for e in entries
        )

    template = MORNING_DIGEST_TEMPLATE if mode == "morning" else EVENING_DIGEST_TEMPLATE
    prompt = template.format(
        date=datetime.now().strftime("%A, %B %d %Y"),
        financial_items=fmt(financial_items),
        urgent_items=fmt(urgent_items),
        job_items=fmt(job_items),
        read_items=fmt(read_items),
        handled_count=handled_count,
    )

    digest_text, cost = call_claude(prompt, model=settings.llm_digest, dry_run=dry_run)
    print(f"[Inbox Zero] {mode.title()} digest generated | Cost: ${cost:.4f}")

    digest = DailyDigest(
        date=datetime.now(),
        total_processed=len(triage_results),
        urgent_count=len(urgent_items),
        action_required=urgent_items,
        job_opportunities=job_items,
        to_read=read_items + financial_items,
        unsub_candidates=[],
        already_handled=handled_count,
        digest_text=digest_text,
    )

    if not dry_run:
        _log_digest(digest)
        # Deliver digest to Gmail inbox under DIGESTS/ label
        try:
            from tools.gmail_client import deliver_digest_to_inbox, _digest_to_html
            label_name = f"DIGESTS/{mode.title()}"
            date_str = datetime.now().strftime("%A, %B %d %Y")
            subject = f"[{mode.title()} Digest] {date_str}"
            html = _digest_to_html(digest.digest_text, mode, date_str)
            # service is not available here — delivered from main.py
            digest._pending_gmail_delivery = {
                "subject": subject,
                "html": html,
                "label": label_name,
            }
        except Exception as e:
            print(f"⚠️  Could not prepare Gmail delivery: {e}")

    return digest

