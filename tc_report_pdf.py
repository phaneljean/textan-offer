"""
tc_report_pdf.py -- Renders a TC File Check result as an actual downloadable
PDF, matching the polish of the branded HTML email (see tc_check_email.py's
format_reply_html) instead of the plain .txt file /tc-check used to hand
back. Takes only the already-computed issue list the browser already has
(same data the .txt download used) -- never re-reads or re-uploads the
original TREC file, so this stays consistent with "your file is never
stored": nothing server-side needs the PDF itself, only the check results.
"""
import io
from datetime import datetime
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, Color

from cover_page import _draw_brand, _draw_rounded_rect, _wrap_text

# TC Check's own brand teal (matches --accent on /tc-check), not the offer
# generator's black/charcoal -- this report should read as coming from the
# TC Check product page a TC actually visited, not the contract-drafting side.
ACCENT = HexColor("#0b5d52")
ACCENT_TINT = HexColor("#E7F3F1")
BLOCKER_COLOR = HexColor("#dc2626")
BLOCKER_BG = HexColor("#fef2f2")
WARNING_COLOR = HexColor("#b45309")
WARNING_BG = HexColor("#fffbeb")
COMPLETE_COLOR = HexColor("#047857")
COMPLETE_BG = HexColor("#ecfdf5")
TEXT_PRIMARY = HexColor("#0f1f2f")
TEXT_MUTED = HexColor("#5a6b7a")
TEXT_DIM = HexColor("#8a9aa9")
DIVIDER = HexColor("#e5e7eb")

MARGIN = 0.65 * inch
ROW_FONT_SIZE = 9.5
ROW_LINE_HEIGHT = 0.16 * inch
ROW_GAP = 0.14 * inch
BOTTOM_LIMIT = 0.9 * inch  # leave room for the footer on every page


def _draw_header(c, width, height, page_num):
    """Brand mark + running title, repeated on every page (page 1 gets the
    full title block drawn separately below it -- see generate())."""
    y = height - 0.6 * inch
    _draw_brand(c, MARGIN, y)
    c.setFillColor(TEXT_PRIMARY)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(MARGIN + 30, y - 7, "TxtAnOffer")
    if page_num > 1:
        c.setFillColor(TEXT_DIM)
        c.setFont("Helvetica", 8)
        c.drawRightString(width - MARGIN, y - 3, f"TC File Check — page {page_num}")
    return y - 0.7 * inch


def _draw_footer(c, width, page_num, page_count):
    c.setStrokeColor(DIVIDER)
    c.setLineWidth(0.5)
    c.line(MARGIN, 0.65 * inch, width - MARGIN, 0.65 * inch)
    c.setFillColor(TEXT_DIM)
    c.setFont("Helvetica", 7)
    c.drawString(MARGIN, 0.48 * inch, "Checked with TC Check by TxtAnOffer · txtanoffer.com/tc-check · Not affiliated with TREC")
    c.drawRightString(width - MARGIN, 0.48 * inch, f"Page {page_num} of {page_count}")


def _status_banner(complete: bool, blocker_count: int, total_issues: int):
    if complete:
        return "All checked fields are filled in.", COMPLETE_COLOR, COMPLETE_BG
    if blocker_count > 0:
        label = f"{blocker_count} title-blocking error{'' if blocker_count == 1 else 's'} found"
        return label, BLOCKER_COLOR, BLOCKER_BG
    label = f"{total_issues} issue{'' if total_issues == 1 else 's'} found"
    return label, WARNING_COLOR, WARNING_BG


def generate_tc_report_pdf(filename: str, issues: list, generated_at=None) -> bytes:
    generated_at = generated_at or datetime.now()
    blockers = [i for i in issues if i.get("severity") == "blocker"]
    warnings = [i for i in issues if i.get("severity") == "warning"]
    complete = not issues

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    content_w = width - 2 * MARGIN

    # Pass 1: lay out every row (with pagination) onto a list of pages so
    # the footer's "Page X of N" is accurate -- reportlab has no way to
    # know the final page count until everything's already been drawn.
    def build_rows():
        rows = []  # (kind, ...) where kind is 'section' or 'issue'
        if blockers:
            rows.append(("section", "Critical deal blockers"))
            for issue in blockers:
                rows.append(("issue", "BLOCKER", BLOCKER_COLOR, BLOCKER_BG, issue.get("message", "")))
        if warnings:
            rows.append(("section", "Also worth fixing"))
            for issue in warnings:
                rows.append(("issue", "WARNING", WARNING_COLOR, WARNING_BG, issue.get("message", "")))
        return rows

    rows = build_rows()

    # Render once to discover how many pages this takes, then again to draw
    # accurate footers -- cheap for a checklist-sized document (never more
    # than a couple dozen rows) and simpler than threading page-count state
    # through a single pass.
    def render(final: bool, known_page_count: int = 1) -> int:
        page_num = 1
        y = _draw_title_block(c, width, height, filename, generated_at, complete, len(blockers), len(issues))
        for kind, *rest in rows:
            row_h = ROW_LINE_HEIGHT + ROW_GAP if kind == "section" else _issue_row_height(c, rest[-1], content_w)
            if y - row_h < BOTTOM_LIMIT:
                if final:
                    _draw_footer(c, width, page_num, known_page_count)
                c.showPage()
                page_num += 1
                y = _draw_header(c, width, height, page_num)
            if kind == "section":
                y = _draw_section_heading(c, rest[0], MARGIN, y)
            else:
                tag, color, bg, message = rest
                y = _draw_issue_row(c, tag, color, bg, message, MARGIN, y, content_w)
        if not rows:
            c.setFillColor(TEXT_MUTED)
            c.setFont("Helvetica", 10)
            c.drawString(MARGIN, y, "Nothing to fix — this file is ready.")
        if final:
            _draw_footer(c, width, page_num, known_page_count)
        return page_num

    total_pages = render(final=False)
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    render(final=True, known_page_count=total_pages)

    c.save()
    buffer.seek(0)
    return buffer.getvalue()


def _draw_title_block(c, width, height, filename, generated_at, complete, blocker_count, total_issues) -> float:
    y = _draw_header(c, width, height, page_num=1)
    cx = width / 2
    content_w = width - 2 * MARGIN

    c.setFillColor(TEXT_PRIMARY)
    c.setFont("Helvetica-Bold", 17)
    c.drawString(MARGIN, y, "TC File Check — Audit Report")
    y -= 0.26 * inch
    c.setFillColor(TEXT_MUTED)
    c.setFont("Helvetica", 9)
    c.drawString(MARGIN, y, f"{filename} · Checked {generated_at.strftime('%B %d, %Y %I:%M %p')}")
    y -= 0.35 * inch

    label, color, bg = _status_banner(complete, blocker_count, total_issues)
    banner_h = 0.4 * inch
    _draw_rounded_rect(c, MARGIN, y - banner_h + 0.1 * inch, content_w, banner_h, r=8, fill_color=bg)
    c.setFillColor(color)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(MARGIN + 0.2 * inch, y - 0.13 * inch, label)
    y -= banner_h + 0.3 * inch
    return y


def _issue_row_height(c, message, content_w) -> float:
    lines = _wrap_text(c, message, "Helvetica", ROW_FONT_SIZE, content_w - 1.1 * inch)
    return max(1, len(lines)) * ROW_LINE_HEIGHT + ROW_GAP


def _draw_section_heading(c, heading, margin, y) -> float:
    c.setFillColor(TEXT_DIM)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(margin, y, heading.upper())
    y -= 0.06 * inch
    c.setStrokeColor(DIVIDER)
    c.setLineWidth(0.5)
    c.line(margin, y, letter[0] - margin, y)
    return y - 0.22 * inch


def _draw_issue_row(c, tag, color, bg, message, margin, y, content_w) -> float:
    tag_w = c.stringWidth(tag, "Helvetica-Bold", 6.5) + 12
    _draw_rounded_rect(c, margin, y - 8, tag_w, 13, r=6, fill_color=bg)
    c.setFillColor(color)
    c.setFont("Helvetica-Bold", 6.5)
    c.drawString(margin + 6, y - 4, tag)

    lines = _wrap_text(c, message, "Helvetica", ROW_FONT_SIZE, content_w - 1.1 * inch)
    text_x = margin + tag_w + 12
    c.setFillColor(TEXT_PRIMARY)
    c.setFont("Helvetica", ROW_FONT_SIZE)
    line_y = y
    for line in lines:
        c.drawString(text_x, line_y, line)
        line_y -= ROW_LINE_HEIGHT
    return line_y - ROW_GAP
