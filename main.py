"""
main.py — Inbox Zero CLI entry point

Usage:
  python main.py --triage                   Fetch + triage unread emails, apply labels
  python main.py --digest                   Generate daily digest summary
  python main.py --triage --digest          Full run: triage + digest in one shot
  python main.py --draft <message_id>       Generate draft reply for a specific email
  python main.py --summarize <message_id>   Deep financial summary of a specific newsletter
  python main.py --unsub                    Show unsubscribe candidates for confirmation
  python main.py --backfill --limit 500     Triage and label existing inbox emails (bulk)
  python main.py --dry-run                  Simulate without touching Gmail or API
  python main.py --pdf --open               Save output as PDF and open in Preview
"""

import argparse
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import settings
from tools.gmail_client import get_gmail_service, fetch_unread_emails
from tools.triage import run_triage
from tools.digest import generate_daily_digest, analyze_unsubscribe_candidates
from tools.drafter import generate_draft, summarize_thread
from tools.coach import call_claude
from prompts import FINANCIAL_SUMMARY_TEMPLATE
from tools.sender_memory import set_sender_override, get_memory_stats, suggest_new_labels


# ─── Known financial newsletter senders ──────────────────────────────────────
FINANCIAL_SENDERS = [
    "dailyshot", "phil rosen", "axios markets", "macro compass",
    "concoda", "doomberg", "bankless", "the block", "messari",
    "morning brew", "bloomberg", "wsj", "ft.com", "reuters",
    "coindesk", "decrypt", "the defiant", "pomp", "marko kolanovic",
    "lyn alden", "real vision", "grant williams", "raoul pal",
]


def is_financial_content(email) -> bool:
    sender_lower = (email.sender + email.sender_email).lower()
    return any(s in sender_lower for s in FINANCIAL_SENDERS)


def cmd_triage(service, dry_run=False, fetch_limit=50) -> dict:
    print(f"\n[Inbox Zero] Fetching up to {fetch_limit} unread emails...")
    emails = [] if dry_run else fetch_unread_emails(service, limit=fetch_limit)

    if not emails:
        print("📭 Inbox is empty or all emails already labeled.")
        return {"processed": 0, "results": [], "emails": [], "total_cost": 0.0}

    print(f"📬 Found {len(emails)} unread emails")
    result = run_triage(service, emails, dry_run=dry_run)
    result["emails"] = emails
    return result


def cmd_backfill(service, dry_run=False, fetch_limit=500, batch_pause=2) -> None:
    """
    Bulk triage existing inbox — processes all email in batches.
    Includes a pause between batches to avoid Gmail API rate limits.
    """
    print(f"\n[Inbox Zero] BACKFILL MODE — processing up to {fetch_limit} inbox emails")
    print("This will label and organize your existing inbox. No emails will be deleted.")
    print("─" * 60)

    if not dry_run:
        confirm = input(f"\nProceed with backfill of up to {fetch_limit} emails? (y/n): ").strip().lower()
        if confirm != "y":
            print("Backfill cancelled.")
            return

    # Fetch ALL inbox emails (not just unread)
    print(f"\n[Inbox Zero] Fetching up to {fetch_limit} inbox emails...")

    try:
        result = service.users().messages().list(
            userId="me",
            labelIds=["INBOX"],
            maxResults=fetch_limit
        ).execute()
        messages = result.get("messages", [])
    except Exception as e:
        print(f"❌ Failed to fetch inbox: {e}")
        return

    if not messages:
        print("📭 No emails found in inbox.")
        return

    print(f"📬 Found {len(messages)} inbox emails to process")

    # Parse into EmailSummary objects
    from tools.gmail_client import _parse_message
    from tools.triage import run_triage, ALL_LABELS
    from tools.gmail_client import ensure_labels_exist

    emails = []
    print("Parsing emails...")
    for i, msg in enumerate(messages):
        summary = _parse_message(service, msg["id"])
        if summary:
            emails.append(summary)
        if (i + 1) % 50 == 0:
            print(f"  Parsed {i+1}/{len(messages)}...")

    print(f"✅ Parsed {len(emails)} emails")

    if not dry_run:
        label_map = ensure_labels_exist(service, ALL_LABELS)
    else:
        label_map = {}

    # Process in batches with pause to respect rate limits
    from tools.triage import triage_batch, apply_triage_results
    batch_size = settings.triage_batch_size
    total_cost = 0.0
    total_labeled = 0

    for i in range(0, len(emails), batch_size):
        batch = emails[i:i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(emails) + batch_size - 1) // batch_size

        print(f"\n  Batch {batch_num}/{total_batches} ({len(batch)} emails)...")
        results, cost = triage_batch(batch, dry_run=dry_run)
        total_cost += cost

        if not dry_run and results:
            counts = apply_triage_results(service, results, label_map)
            total_labeled += sum(v for k, v in counts.items() if k != "errors")

        # Pause between batches to avoid hitting Gmail API rate limits
        if i + batch_size < len(emails) and not dry_run:
            time.sleep(batch_pause)

    print(f"\n{'─'*60}")
    print(f"✅ Backfill complete")
    print(f"   Emails processed : {len(emails)}")
    print(f"   Emails labeled   : {total_labeled}")
    print(f"   Total API cost   : ${total_cost:.4f}")
    print(f"\nCheck your Gmail sidebar — KRU/ labels are now applied to your inbox.")


def cmd_digest(service, triage_data: dict, dry_run=False, save_pdf=False, open_pdf=False, mode="auto"):
    emails = triage_data.get("emails", [])
    results = triage_data.get("results", [])

    if not results:
        print("⚠️  No triage results to digest. Run --triage first.")
        return

    print("\n[Inbox Zero] Generating daily digest...")
    from tools.digest import generate_timed_digest
    digest = generate_timed_digest(emails, results, mode=mode, dry_run=dry_run, service=service)

    print("\n" + "─" * 60)
    print(digest.digest_text)
    print("─" * 60)

    if digest.urgent_count > 0:
        print(f"\n🚨 {digest.urgent_count} URGENT emails require same-day response")
    if digest.job_opportunities:
        print(f"\n💼 {len(digest.job_opportunities)} job opportunities flagged")

    if save_pdf and not dry_run:
        from tools.pdf_reporter import save_digest_pdf
        save_digest_pdf(digest, open_after=open_pdf)

    # Deliver digest to Gmail inbox
    if not dry_run and service and hasattr(digest, '_pending_gmail_delivery'):
        from tools.gmail_client import deliver_digest_to_inbox
        d = digest._pending_gmail_delivery
        deliver_digest_to_inbox(
            service=service,
            subject=d["subject"],
            body_html=d["html"],
            label_name=d["label"],
        )


def cmd_summarize(service, message_id: str, dry_run=False):
    """Deep financial summary of a specific newsletter email."""
    print(f"\n[Inbox Zero] Fetching email {message_id}...")
    emails = fetch_unread_emails(service, limit=200)
    email = next((e for e in emails if e.message_id == message_id), None)

    if not email:
        print(f"❌ Email {message_id} not found.")
        return

    # Fetch full body
    from tools.gmail_client import fetch_thread
    full_body = fetch_thread(service, email.thread_id) if not dry_run else email.body_preview

    prompt = FINANCIAL_SUMMARY_TEMPLATE.format(
        sender=f"{email.sender} <{email.sender_email}>",
        subject=email.subject,
        body=full_body[:3000],
    )

    response, cost = call_claude(prompt, model=settings.llm_digest, dry_run=dry_run)
    print(f"\n📊 Financial Summary — {email.subject}")
    print("─" * 60)
    print(response)
    print("─" * 60)
    print(f"[Cost: ${cost:.4f}]")


def cmd_draft(service, message_id: str, dry_run=False):
    print(f"\n[Inbox Zero] Fetching email {message_id}...")
    emails = fetch_unread_emails(service, limit=100)
    email = next((e for e in emails if e.message_id == message_id), None)

    if not email:
        print(f"❌ Email {message_id} not found in unread inbox.")
        return

    instructions = input(f"\nDraft instructions for '{email.subject}' (or Enter for default): ").strip()
    generate_draft(service, email, instructions=instructions, dry_run=dry_run)


def cmd_unsub(service, triage_data: dict, dry_run=False):
    emails = triage_data.get("emails", [])
    results = triage_data.get("results", [])

    if not results:
        print("⚠️  No triage results. Run --triage first.")
        return

    candidates = analyze_unsubscribe_candidates(emails, results, dry_run=dry_run)

    if not candidates:
        return

    print(f"\n🗑️  Found {len(candidates)} unsubscribe candidates:\n")
    for i, c in enumerate(candidates, 1):
        msg_count = len(c.get("message_ids", []))
        print(f"  {i}. {c.get('sender_name', c.get('sender_email'))} "
              f"({msg_count} emails) → {c.get('recommended_action')} "
              f"| {c.get('reason')}")

    print("\n⚠️  No actions taken. Review list above and confirm manually.")


def main():
    parser = argparse.ArgumentParser(
        description="Inbox Zero — Gmail triage and drafting agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    parser.add_argument("--triage",    action="store_true")
    parser.add_argument("--digest",    action="store_true")
    parser.add_argument("--draft",     type=str, help="Message ID to draft reply for")
    parser.add_argument("--summarize", type=str, help="Message ID for deep financial summary")
    parser.add_argument("--unsub",     action="store_true")
    parser.add_argument("--backfill",  action="store_true", help="Bulk triage existing inbox")
    parser.add_argument("--dry-run",   action="store_true")
    parser.add_argument("--pdf",       action="store_true")
    parser.add_argument("--open",      action="store_true")
    parser.add_argument("--mode",      type=str, default="auto", choices=["morning","evening","auto"], help="Digest mode")
    parser.add_argument("--limit",     type=int, default=50, help="Max emails to fetch")
    parser.add_argument("--override",   type=str, nargs=2, metavar=("EMAIL", "PRIORITY"), help="Override sender priority: --override sender@email.com high")
    parser.add_argument("--memory",     action="store_true", help="Show sender memory stats and label suggestions")

    args = parser.parse_args()

    if not args.dry_run and not settings.anthropic_api_key:
        print("❌ ANTHROPIC_API_KEY not set in ~/.env")
        sys.exit(1)

    print("\n📧 Inbox Zero" + (" [DRY RUN]" if args.dry_run else ""))

    service = None
    if not args.dry_run:
        try:
            service = get_gmail_service()
            print("✅ Gmail authenticated")
        except Exception as e:
            print(f"❌ Gmail auth failed: {e}")
            sys.exit(1)

    triage_data = {"emails": [], "results": []}

    if hasattr(args, "override") and args.override:
        set_sender_override(args.override[0], args.override[1])
        return

    if hasattr(args, "memory") and args.memory:
        cmd_memory()
        return

    if args.backfill:
        cmd_backfill(service, dry_run=args.dry_run, fetch_limit=args.limit)
        return

    if args.summarize:
        cmd_summarize(service, args.summarize, dry_run=args.dry_run)
        return

    if args.triage or (not args.draft and not args.unsub and not args.digest):
        triage_data = cmd_triage(service, dry_run=args.dry_run, fetch_limit=args.limit)

    if args.digest:
        if not triage_data.get("emails") and not args.dry_run:
            emails = fetch_unread_emails(service, limit=args.limit)
            triage_data["emails"] = emails
        cmd_digest(service, triage_data,
                   dry_run=args.dry_run, save_pdf=args.pdf, open_pdf=args.open, mode=args.mode)

    if args.draft:
        cmd_draft(service, args.draft, dry_run=args.dry_run)

    if args.unsub:
        cmd_unsub(service, triage_data, dry_run=args.dry_run)


def cmd_memory():
    """Print sender memory stats and label suggestions."""
    stats = get_memory_stats()
    print(f"\n📊 Sender Memory — {stats['total_senders']} senders tracked")
    print("─" * 60)

    if stats["total_senders"] == 0:
        print("No memory yet — run --triage first.")
        return

    print(f"\nPriority breakdown: {stats['priority_breakdown']}")

    print(f"\nTop senders by volume:")
    for email, count, priority, name in stats["top_senders"]:
        print(f"  {name[:30]:30} {count:3} emails  avg: {priority}")

    if stats["unsub_candidates"]:
        print(f"\n🗑️  Persistent unsub candidates:")
        for email, name, count in stats["unsub_candidates"]:
            print(f"  {name[:30]:30} flagged {count}x")

    if stats["overridden_senders"]:
        print(f"\n✏️  Manual overrides active:")
        for email, name, priority in stats["overridden_senders"]:
            print(f"  {name[:30]:30} → {priority}")

    suggestions = suggest_new_labels()
    if suggestions:
        print(f"\n💡 Label suggestions:")
        for s in suggestions:
            print(f"  {s['reason']}")
            for email, name, count in s["senders"][:3]:
                print(f"    • {name} ({count} emails)")


if __name__ == "__main__":
    main()

