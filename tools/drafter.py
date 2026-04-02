"""
tools/drafter.py — Draft reply generation
Generates complete, ready-to-send drafts in Vikram's voice.
Saves to Gmail drafts — NEVER sends autonomously.
"""

from typing import Optional
from models import EmailSummary, DraftReply
from tools.coach import call_claude
from tools.gmail_client import fetch_thread, create_draft
from prompts import DRAFT_REPLY_TEMPLATE, SUMMARIZE_THREAD_TEMPLATE
from config import settings


def generate_draft(
    service,
    email: EmailSummary,
    instructions: str = "",
    dry_run: bool = False
) -> Optional[DraftReply]:
    """
    Generate a draft reply for an email.
    Fetches thread context, generates reply, saves to Gmail drafts.

    instructions: Optional guidance — e.g. "Express interest, ask about timeline"
    """
    # Fetch thread for context
    thread_context = ""
    if email.thread_length > 1 and not dry_run:
        thread_context = fetch_thread(service, email.thread_id)

    prompt = DRAFT_REPLY_TEMPLATE.format(
        sender=f"{email.sender} <{email.sender_email}>",
        subject=email.subject,
        body=email.body_preview,
        thread_context=thread_context or "No prior thread.",
        instructions=instructions or "Draft an appropriate professional reply.",
    )

    draft_text, cost = call_claude(prompt, model=settings.llm_draft, dry_run=dry_run)

    print(f"\n[Inbox Zero] Draft generated | Cost: ${cost:.4f}")
    print("─" * 50)
    print(draft_text)
    print("─" * 50)

    if dry_run:
        return None

    # Save to Gmail drafts
    reply_subject = email.subject if email.subject.startswith("Re:") else f"Re: {email.subject}"
    draft_id = create_draft(
        service=service,
        to=email.sender_email,
        subject=reply_subject,
        body=draft_text,
        thread_id=email.thread_id,
    )

    word_count = len(draft_text.split())

    return DraftReply(
        message_id=email.message_id,
        thread_id=email.thread_id,
        subject=reply_subject,
        to=email.sender_email,
        body=draft_text,
        word_count=word_count,
        tone_check="direct",
    )


def summarize_thread(
    service,
    email: EmailSummary,
    dry_run: bool = False
) -> str:
    """Summarize an email thread into a concise brief."""
    thread_text = fetch_thread(service, email.thread_id) if not dry_run else email.body_preview

    prompt = SUMMARIZE_THREAD_TEMPLATE.format(thread_text=thread_text)
    summary, cost = call_claude(prompt, model=settings.llm_digest, dry_run=dry_run)

    print(f"[Inbox Zero] Thread summarized | Cost: ${cost:.4f}")
    return summary
