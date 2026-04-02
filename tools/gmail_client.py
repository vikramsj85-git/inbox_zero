"""
tools/gmail_client.py — Gmail API interface
Handles OAuth flow, fetching, labeling, archiving, and drafting.
All destructive actions require confirmed=True — never autonomous.
"""

import json
import base64
import email as email_lib
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from email.mime.text import MIMEText

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import CREDENTIALS_FILE, TOKEN_FILE, GMAIL_SCOPES
from models import EmailSummary


# ─── Auth ────────────────────────────────────────────────────────────────────

def get_gmail_service():
    """
    Authenticate and return Gmail API service.
    First run: opens browser for OAuth consent.
    Subsequent runs: uses cached token.json silently.
    """
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)

    # Refresh or re-authenticate if needed
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"credentials.json not found at {CREDENTIALS_FILE}\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials"
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), GMAIL_SCOPES
            )
            # Opens browser for consent on first run
            creds = flow.run_local_server(port=0)

        # Cache token for future runs
        TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ─── Fetch ───────────────────────────────────────────────────────────────────

def fetch_unread_emails(service, limit: int = 50) -> List[EmailSummary]:
    """
    Fetch unread emails from inbox.
    Returns compact EmailSummary objects — not raw API blobs.
    """
    try:
        result = service.users().messages().list(
            userId="me",
            labelIds=["INBOX", "UNREAD"],
            maxResults=limit
        ).execute()

        messages = result.get("messages", [])
        summaries = []

        for msg in messages:
            summary = _parse_message(service, msg["id"])
            if summary:
                summaries.append(summary)

        return summaries

    except HttpError as e:
        print(f"❌ Gmail API error fetching emails: {e}")
        return []


def fetch_thread(service, thread_id: str) -> str:
    """Fetch full thread and return as readable text for Claude context."""
    try:
        thread = service.users().threads().get(
            userId="me", id=thread_id, format="full"
        ).execute()

        parts = []
        for msg in thread.get("messages", []):
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            body = _extract_body(msg["payload"])
            parts.append(
                f"From: {headers.get('From', 'Unknown')}\n"
                f"Date: {headers.get('Date', 'Unknown')}\n"
                f"Subject: {headers.get('Subject', 'No subject')}\n\n"
                f"{body[:1000]}"
            )

        return "\n\n---\n\n".join(parts)

    except HttpError as e:
        return f"[Error fetching thread: {e}]"


def _parse_message(service, message_id: str) -> Optional[EmailSummary]:
    """Parse a raw Gmail message into an EmailSummary."""
    try:
        msg = service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()

        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        sender_raw = headers.get("From", "Unknown <unknown@unknown.com>")

        # Parse "Name <email>" format
        if "<" in sender_raw:
            sender_name = sender_raw.split("<")[0].strip().strip('"')
            sender_email = sender_raw.split("<")[1].rstrip(">")
        else:
            sender_name = sender_raw
            sender_email = sender_raw

        body = _extract_body(msg["payload"])
        date_str = headers.get("Date", "")

        try:
            date = datetime.strptime(date_str[:25].strip(), "%a, %d %b %Y %H:%M:%S")
        except Exception:
            date = datetime.now()

        return EmailSummary(
            message_id=message_id,
            thread_id=msg["threadId"],
            sender=sender_name,
            sender_email=sender_email,
            subject=headers.get("Subject", "(no subject)"),
            date=date,
            snippet=msg.get("snippet", "")[:200],
            body_preview=body[:500],
            has_attachments=_has_attachments(msg["payload"]),
            is_reply="Re:" in headers.get("Subject", ""),
            thread_length=1,
        )

    except Exception as e:
        print(f"⚠️  Could not parse message {message_id}: {e}")
        return None


def _extract_body(payload: dict) -> str:
    """Recursively extract text body from Gmail payload."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        result = _extract_body(part)
        if result:
            return result

    return ""


def _has_attachments(payload: dict) -> bool:
    for part in payload.get("parts", []):
        if part.get("filename"):
            return True
    return False


# ─── Labels ──────────────────────────────────────────────────────────────────

def ensure_labels_exist(service, label_names: List[str]) -> dict:
    """
    Create labels if they don't exist. Returns {name: id} mapping.
    Gmail requires label IDs for all operations.
    """
    existing = service.users().labels().list(userId="me").execute()
    existing_map = {l["name"]: l["id"] for l in existing.get("labels", [])}
    label_map = {}

    for name in label_names:
        if name in existing_map:
            label_map[name] = existing_map[name]
        else:
            created = service.users().labels().create(
                userId="me",
                body={"name": name, "labelListVisibility": "labelShow",
                      "messageListVisibility": "show"}
            ).execute()
            label_map[name] = created["id"]
            print(f"✅ Created label: {name}")

    return label_map


def apply_labels(service, message_id: str, label_ids: List[str]) -> bool:
    """Apply labels to a message. Safe — never removes existing labels."""
    try:
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": label_ids}
        ).execute()
        return True
    except HttpError as e:
        print(f"⚠️  Failed to label {message_id}: {e}")
        return False


# ─── Archive ─────────────────────────────────────────────────────────────────

def archive_message(service, message_id: str, confirmed: bool = False) -> bool:
    """
    Archive a message (remove INBOX label).
    Requires confirmed=True — never called autonomously.
    """
    if not confirmed:
        raise PermissionError(
            f"archive_message requires confirmed=True. "
            f"Message {message_id} was NOT archived."
        )
    try:
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["INBOX"]}
        ).execute()
        return True
    except HttpError as e:
        print(f"⚠️  Failed to archive {message_id}: {e}")
        return False


def bulk_delete(service, message_ids: List[str], confirmed: bool = False) -> int:
    """
    Move messages to trash. Requires confirmed=True.
    Returns count of successfully trashed messages.
    """
    if not confirmed:
        raise PermissionError(
            f"bulk_delete requires confirmed=True. "
            f"{len(message_ids)} messages were NOT deleted."
        )
    success = 0
    for msg_id in message_ids:
        try:
            service.users().messages().trash(userId="me", id=msg_id).execute()
            success += 1
        except HttpError as e:
            print(f"⚠️  Failed to trash {msg_id}: {e}")
    return success


# ─── Drafts ──────────────────────────────────────────────────────────────────

def create_draft(service, to: str, subject: str, body: str,
                 thread_id: Optional[str] = None) -> Optional[str]:
    """
    Save a draft reply. NEVER sends — drafts only.
    Returns draft ID if successful.
    """
    try:
        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        draft_body = {"message": {"raw": raw}}

        if thread_id:
            draft_body["message"]["threadId"] = thread_id

        draft = service.users().drafts().create(
            userId="me", body=draft_body
        ).execute()

        print(f"✅ Draft saved (ID: {draft['id']}) — NOT sent")
        return draft["id"]

    except HttpError as e:
        print(f"❌ Failed to create draft: {e}")
        return None


# ─── Digest Inbox Delivery ───────────────────────────────────────────────────

def deliver_digest_to_inbox(
    service,
    subject: str,
    body_html: str,
    label_name: str,
    user_email: str = "me"
) -> Optional[str]:
    """
    Insert a digest email directly into Gmail inbox under a DIGESTS/ label.
    Uses messages.insert — no send scope required, no external delivery.
    Returns message ID if successful.
    """
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText as MIMETextPart

    try:
        # Ensure the label exists
        label_map = ensure_labels_exist(service, [label_name])
        label_id = label_map.get(label_name)

        # Build HTML email to self
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = "Inbox Zero Agent <me>"
        msg["To"] = "me"

        # Plain text fallback
        plain = body_html.replace("<br>", "\n").replace("</p>", "\n")
        import re
        plain = re.sub(r"<[^>]+>", "", plain)
        msg.attach(MIMETextPart(plain, "plain"))
        msg.attach(MIMETextPart(body_html, "html"))

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        # Insert directly into Gmail — bypasses send, no external delivery
        result = service.users().messages().insert(
            userId="me",
            body={
                "raw": raw,
                "labelIds": ["INBOX", label_id] if label_id else ["INBOX"],
            }
        ).execute()

        print(f"✅ Digest delivered to Gmail inbox under {label_name} (ID: {result['id']})")
        return result["id"]

    except HttpError as e:
        print(f"❌ Failed to deliver digest to Gmail: {e}")
        return None


def _digest_to_html(digest_text: str, mode: str, date_str: str) -> str:
    """Convert plain digest text to clean HTML for Gmail display."""
    mode_emoji = "🌅" if mode == "morning" else "🌙"
    mode_label = "Morning" if mode == "morning" else "Evening"

    lines = digest_text.strip().split("\n")
    html_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            html_lines.append("<br>")
        elif stripped.isupper() and len(stripped) < 60:
            html_lines.append(f"<h3 style='color:#1A3A5C;margin:16px 0 4px 0;'>{stripped}</h3>")
        elif stripped.startswith(("1.", "2.", "3.", "4.", "5.")):
            html_lines.append(f"<p style='margin:4px 0;'>{stripped}</p>")
        elif stripped.startswith("-") or stripped.startswith("•"):
            html_lines.append(f"<p style='margin:2px 0 2px 16px;'>{stripped}</p>")
        else:
            html_lines.append(f"<p style='margin:6px 0;'>{stripped}</p>")

    body = "\n".join(html_lines)

    return f"""
<html><body style="font-family:Helvetica,Arial,sans-serif;max-width:680px;margin:0 auto;color:#1A1A1A;">
  <div style="background:#1A3A5C;padding:16px 24px;border-radius:6px 6px 0 0;">
    <h2 style="color:white;margin:0;">{mode_emoji} Inbox Zero — {mode_label} Digest</h2>
    <p style="color:#8AB0D0;margin:4px 0 0 0;font-size:13px;">{date_str}</p>
  </div>
  <div style="border:1px solid #DDDDDD;border-top:none;padding:20px 24px;border-radius:0 0 6px 6px;">
    {body}
  </div>
  <p style="font-size:11px;color:#AAAAAA;text-align:center;margin-top:12px;">
    Generated by Inbox Zero · Powered by Anthropic Claude
  </p>
</body></html>
"""


# ─── Digest Email ─────────────────────────────────────────────────────────────

def send_digest_to_self(service, subject: str, body_html: str,
                        label_name: str = "DIGESTS") -> bool:
    """
    Send the digest as an email to yourself and apply the DIGESTS label.
    This makes it appear as a readable email in your Gmail inbox.
    """
    import base64
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    # Get user's email address
    profile = service.users().getProfile(userId="me").execute()
    user_email = profile["emailAddress"]

    # Build HTML email
    msg = MIMEMultipart("alternative")
    msg["to"] = user_email
    msg["from"] = user_email
    msg["subject"] = subject

    # Plain text fallback
    plain = body_html.replace("<br>", "\n").replace("</p>", "\n\n")
    import re
    plain = re.sub(r"<[^>]+>", "", plain)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    try:
        sent = service.users().messages().send(
            userId="me",
            body={"raw": raw}
        ).execute()

        # Apply DIGESTS label
        label_map = ensure_labels_exist(service, [label_name])
        if label_name in label_map:
            service.users().messages().modify(
                userId="me",
                id=sent["id"],
                body={"addLabelIds": [label_map[label_name]]}
            ).execute()

        print(f"✅ Digest emailed to {user_email} → labeled {label_name}")
        return True

    except HttpError as e:
        print(f"❌ Failed to send digest email: {e}")
        return False


def format_digest_as_html(digest_text: str, title: str, mode: str) -> str:
    """Convert plain text digest into clean HTML email."""
    icon = "🌅" if mode == "morning" else "🌙"
    color = "#1A3A5C"

    lines = digest_text.strip().split("\n")
    html_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            html_lines.append("<br>")
        elif stripped.isupper() and len(stripped) < 60:
            html_lines.append(f'<h3 style="color:{color};border-bottom:1px solid #ddd;padding-bottom:4px">{stripped}</h3>')
        else:
            html_lines.append(f'<p style="margin:4px 0;line-height:1.6">{stripped}</p>')

    body = "\n".join(html_lines)

    return f"""
<html><body style="font-family:Helvetica,Arial,sans-serif;max-width:680px;margin:0 auto;padding:20px;color:#2C2C2C">
  <div style="background:{color};color:white;padding:16px 20px;border-radius:6px 6px 0 0">
    <h2 style="margin:0;font-size:18px">{icon} {title}</h2>
  </div>
  <div style="border:1px solid #ddd;border-top:none;padding:20px;border-radius:0 0 6px 6px">
    {body}
  </div>
  <p style="color:#999;font-size:11px;text-align:center;margin-top:12px">
    Inbox Zero · Powered by Anthropic Claude
  </p>
</body></html>
"""
