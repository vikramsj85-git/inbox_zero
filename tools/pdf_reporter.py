"""
tools/pdf_reporter.py — PDF report generator for Inbox Zero
Produces clean daily digest and triage summary PDFs.
"""

import json
import re
from datetime import date, datetime
from pathlib import Path
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER

REPORTS_DIR = Path(__file__).parent.parent / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

BLACK      = colors.HexColor("#1A1A1A")
BLUE       = colors.HexColor("#1A3A5C")
RED        = colors.HexColor("#C0392B")
GREEN      = colors.HexColor("#27AE60")
DARK_GREY  = colors.HexColor("#2C2C2C")
MID_GREY   = colors.HexColor("#555555")
LIGHT_GREY = colors.HexColor("#F4F4F4")
WHITE      = colors.white


def _styles():
    return {
        "title":   ParagraphStyle("IZTitle",  fontName="Helvetica-Bold", fontSize=22, textColor=BLACK, spaceAfter=4),
        "subtitle":ParagraphStyle("IZSub",    fontName="Helvetica",      fontSize=11, textColor=MID_GREY, spaceAfter=2),
        "section": ParagraphStyle("IZSect",   fontName="Helvetica-Bold", fontSize=12, textColor=BLUE, spaceBefore=14, spaceAfter=6),
        "body":    ParagraphStyle("IZBody",   fontName="Helvetica",      fontSize=10, textColor=DARK_GREY, spaceAfter=5, leading=15),
        "urgent":  ParagraphStyle("IZUrgent", fontName="Helvetica-Bold", fontSize=10, textColor=RED, spaceAfter=4),
        "meta":    ParagraphStyle("IZMeta",   fontName="Helvetica-Oblique", fontSize=9, textColor=MID_GREY, spaceAfter=4),
        "footer":  ParagraphStyle("IZFoot",   fontName="Helvetica",      fontSize=8,  textColor=MID_GREY, alignment=TA_CENTER),
    }


def _divider(color=BLUE):
    return HRFlowable(width="100%", thickness=1.5, color=color, spaceAfter=8, spaceBefore=4)

def _thin():
    return HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#DDDDDD"), spaceAfter=5, spaceBefore=5)


# Section header patterns: ALL CAPS lines or lines like "MARKETS CLOSE:"
_SECTION_HEADER_RE = re.compile(r'^[A-Z][A-Z /\'\-]{3,}:?\s*$')


def _render_digest_text(digest_text: str, story: list, s: dict) -> None:
    """
    Render digest_text intelligently into the story.
    Handles: plain prose sections, ALL CAPS headers, and JSON fallback
    (defensive — prompts now explicitly request plain text).
    """
    text = digest_text.strip()

    # Strip markdown code fences if Claude ignored the instruction
    text = re.sub(r'^```(?:json)?\s*\n?', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n?```\s*$', '', text, flags=re.MULTILINE)
    text = text.strip()

    # Defensive JSON parse — render structured sections cleanly
    if text.startswith('{'):
        try:
            data = json.loads(text)
            _render_digest_json(data, story, s)
            return
        except json.JSONDecodeError:
            pass  # Fall through to plain text rendering

    # Plain text: detect ALL CAPS section headers, render body paragraphs
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            story.append(Spacer(1, 4))
            continue

        if _SECTION_HEADER_RE.match(line):
            story.append(Spacer(1, 8))
            story.append(Paragraph(line.rstrip(':'), s["section"]))
            story.append(_thin())
        else:
            story.append(Paragraph(line, s["body"]))


def _render_digest_json(data: dict, story: list, s: dict) -> None:
    """Render a JSON-structured digest cleanly when plain-text prompting fails."""
    # Map known JSON keys to display labels
    section_labels = {
        'market_brief':         'MARKET BRIEF',
        'markets_close':        'MARKETS CLOSE',
        'todays_priorities':    "TODAY'S PRIORITIES",
        'tomorrows_priorities': "TOMORROW'S PRIORITIES",
        'job_opps':             'JOB OPPORTUNITIES',
        'reading_queue':        'READING QUEUE',
        'follow_ups':           'FOLLOW-UPS NEEDED',
        'evening_read':         'EVENING READ',
    }

    sections = data.get('sections', data)  # tolerate flat or nested structure

    for key, label in section_labels.items():
        section = sections.get(key)
        if not section:
            continue

        story.append(Spacer(1, 8))
        story.append(Paragraph(label, s["section"]))
        story.append(_thin())

        if isinstance(section, dict):
            if 'headline' in section:
                story.append(Paragraph(f"<b>{section['headline']}</b>", s["body"]))
            if 'summary' in section:
                story.append(Paragraph(section['summary'], s["body"]))
            if 'signal_strength' in section:
                story.append(Paragraph(f"Signal strength: <b>{section['signal_strength']}</b>", s["meta"]))
            if 'ranked' in section:
                for item in section['ranked']:
                    story.append(Paragraph(str(item), s["body"]))
            if 'bottom_line' in section:
                story.append(Paragraph(f"<i>{section['bottom_line']}</i>", s["meta"]))
        elif isinstance(section, list):
            for item in section:
                story.append(Paragraph(f"• {item}", s["body"]))
        elif isinstance(section, str):
            story.append(Paragraph(section, s["body"]))


def save_digest_pdf(digest, open_after: bool = False) -> Path:
    """Generate a formatted daily digest PDF."""
    s = _styles()
    report_date = date.today()
    filename = f"{report_date.strftime('%Y%m%d')}_inbox_digest.pdf"
    output_path = REPORTS_DIR / filename

    doc = SimpleDocTemplate(str(output_path), pagesize=letter,
                            leftMargin=0.85*inch, rightMargin=0.85*inch,
                            topMargin=0.85*inch, bottomMargin=0.85*inch)
    story = []

    # Header
    story.append(Paragraph("📧 INBOX ZERO", s["title"]))
    story.append(Paragraph(f"Daily Digest  ·  {report_date.strftime('%A, %B %d, %Y')}", s["subtitle"]))
    story.append(_divider())

    # Stats strip
    stats = [
        ["Processed", "Urgent", "Job Opps", "To Read", "Unsub Candidates"],
        [str(digest.total_processed), str(digest.urgent_count),
         str(len(digest.job_opportunities)), str(len(digest.to_read)),
         str(len(digest.unsub_candidates))],
    ]
    t = Table(stats, colWidths=[1.3*inch]*5)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), BLUE),
        ("TEXTCOLOR",  (0,0), (-1,0), WHITE),
        ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",   (0,0), (-1,0), 9),
        ("BACKGROUND", (0,1), (-1,1), LIGHT_GREY),
        ("FONTNAME",   (0,1), (-1,1), "Helvetica-Bold"),
        ("FONTSIZE",   (0,1), (-1,1), 11),
        ("ALIGN",      (0,0), (-1,-1), "CENTER"),
        ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
        ("PADDING",    (0,0), (-1,-1), 8),
        ("GRID",       (0,0), (-1,-1), 0.5, colors.HexColor("#CCCCCC")),
    ]))
    story.append(t)
    story.append(Spacer(1, 12))

    # Digest narrative
    story.append(Paragraph("EXECUTIVE SUMMARY", s["section"]))
    story.append(_thin())
    _render_digest_text(digest.digest_text, story, s)

    # Urgent items
    if digest.action_required:
        story.append(Paragraph("🚨 URGENT / ACTION REQUIRED", s["section"]))
        story.append(_thin())
        for entry in digest.action_required:
            story.append(Paragraph(f"• {entry.subject} — {entry.sender}", s["urgent"]))
            if entry.action_needed:
                story.append(Paragraph(f"  → {entry.action_needed}", s["meta"]))

    # Job opportunities
    if digest.job_opportunities:
        story.append(Paragraph("💼 JOB OPPORTUNITIES", s["section"]))
        story.append(_thin())
        for entry in digest.job_opportunities:
            story.append(Paragraph(f"• {entry.subject} — {entry.sender}", s["body"]))
            story.append(Paragraph(f"  {entry.one_line_summary}", s["meta"]))

    # To read
    if digest.to_read:
        story.append(Paragraph("📖 TO READ", s["section"]))
        story.append(_thin())
        for entry in digest.to_read:
            story.append(Paragraph(f"• {entry.subject} — {entry.sender}", s["body"]))

    # Unsubscribe candidates
    if digest.unsub_candidates:
        story.append(Paragraph("🗑️ UNSUBSCRIBE CANDIDATES", s["section"]))
        story.append(_thin())
        for entry in digest.unsub_candidates:
            story.append(Paragraph(f"• {entry.sender}: {entry.subject}", s["body"]))

    # Footer
    story.append(Spacer(1, 12))
    story.append(_divider(color=colors.HexColor("#DDDDDD")))
    story.append(Paragraph(
        f"Inbox Zero  ·  {report_date.strftime('%Y-%m-%d')}  ·  Powered by Anthropic Claude",
        s["footer"]
    ))

    doc.build(story)
    print(f"\n📄 Digest PDF saved: {output_path}")

    if open_after:
        import subprocess
        subprocess.run(["open", str(output_path)])

    return output_path
