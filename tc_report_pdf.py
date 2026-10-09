"""
tc_report_pdf.py -- Renders a TC File Check result as a downloadable PDF.

Redesigned 2026-10-07 to match the emailed report (tc_check_email.py):
dark-green header with the white txtanoffer wordmark and a yellow accent bar,
the property address from Section 2A as the headline, every finding listed
with its consequence tag (per-page initials already folded into one line by
the /v1/tc/check response), next steps, the Brokerage archive card, and the
Brokerage audit card. Takes only the already-computed results the browser
has -- never re-reads the original TREC file. The uploaded PDF only lives
in a temp file for the seconds the check takes (deleted in app.py's finally
block), so it isn't available here anyway. Don't describe that as "never
stored": a temp copy does exist briefly, and the site copy says "checked,
then deleted -- no copy kept".
"""
import io
import os
from datetime import datetime
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor

from cover_page import _draw_rounded_rect, _wrap_text
from tc_check_email import _streak_note, _UPSELL_URL, _DISCLAIMER

GREEN_DARK = HexColor("#0a3f3a")
GREEN = HexColor("#0b5d52")
GREEN_TINT = HexColor("#E7F3F1")
GREEN_TEXT_LIGHT = HexColor("#c9dcd8")
YELLOW = HexColor("#f5c242")
YELLOW_TINT = HexColor("#FFF6DA")
YELLOW_TEXT = HexColor("#3a3320")
BLOCKER_COLOR = HexColor("#dc2626")
BLOCKER_BG = HexColor("#fdecec")
WARNING_COLOR = HexColor("#b45309")
WARNING_BG = HexColor("#fdf3e3")
COMPLETE_COLOR = HexColor("#047857")
COMPLETE_BG = HexColor("#ecfdf5")
TEXT_PRIMARY = HexColor("#0f1f2f")
TEXT_BODY = HexColor("#2c3d4d")
TEXT_MUTED = HexColor("#5a6b7a")
TEXT_DIM = HexColor("#8a9aa9")
DIVIDER = HexColor("#e5e7eb")
WHITE = HexColor("#ffffff")

MARGIN = 0.65 * inch
BOTTOM_LIMIT = 0.95 * inch  # room for the footer on every page
ROW_FONT = ("Helvetica", 10)
LINE_H = 0.17 * inch

_LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "logo-wordmark-white.png")

_NEXT_STEPS = [
    "1.  Fill in or correct the items above.",
    "2.  Check it again: forward the corrected file to tc@check.txtanoffer.com, or upload it at txtanoffer.com/tc-check.",
    "3.  Share this with your agent: just forward this report.",
]
_ARCHIVE_HEADING = "Keep every contract on file"
_ARCHIVE_TEXT = ("On the Brokerage plan, forward executed contracts to tc@check.txtanoffer.com from your brokerage "
                 "email or drop them into your archive. Each one is checked and kept 5 years, searchable by address "
                 "\u2014 longer than the 4 years TREC requires brokers to keep transaction records (22 TAC \u00a7535.2). "
                 "txtanoffer.com/archive")
_UPSELL_HEADING = "Every file, every agent, checked automatically"
_UPSELL_TEXT = ("The Brokerage plan checks every offer your agents send before it goes out — $349/mo for your "
                "whole roster. Start with a free audit of your last 20 closed files.")
_UPSELL_CTA = "Get a free 20-file audit →"


def _header(c, width, height, compact=False) -> float:
    """Green band + white wordmark + yellow bar. Returns the y below it."""
    band_h = 0.5 * inch if compact else 0.72 * inch
    top = height
    c.setFillColor(GREEN_DARK)
    c.rect(0, top - band_h, width, band_h, fill=1, stroke=0)
    c.setFillColor(YELLOW)
    c.rect(0, top - band_h - 4, width, 4, fill=1, stroke=0)
    logo_w = 1.15 * inch if compact else 1.45 * inch
    logo_h = logo_w * 120 / 708
    if os.path.exists(_LOGO_PATH):
        c.drawImage(_LOGO_PATH, MARGIN, top - band_h / 2 - logo_h / 2, logo_w, logo_h, mask="auto")
    c.setFillColor(YELLOW)
    c.setFont("Helvetica-Bold", 7.5)
    c.drawRightString(width - MARGIN, top - band_h / 2 - 3, "TC FILE CHECK REPORT")
    return top - band_h - 4 - 0.42 * inch


def _footer(c, width, page_num, page_count, streak_note=""):
    if page_num == page_count and streak_note:
        c.setFillColor(TEXT_DIM)
        c.setFont("Helvetica", 7.5)
        c.drawString(MARGIN, 0.82 * inch, streak_note)
    c.setStrokeColor(DIVIDER)
    c.setLineWidth(0.5)
    c.line(MARGIN, 0.7 * inch, width - MARGIN, 0.7 * inch)
    c.setFillColor(TEXT_DIM)
    c.setFont("Helvetica", 6.5)
    c.drawString(MARGIN, 0.52 * inch, _DISCLAIMER)
    c.drawRightString(width - MARGIN, 0.36 * inch, f"txtanoffer.com  ·  Page {page_num} of {page_count}")


def _title_block(c, width, height, property, files_line, filename, generated_at, issues) -> float:
    y = _header(c, width, height)
    content_w = width - 2 * MARGIN
    prop = property or {}
    if prop.get("address"):
        c.setFillColor(TEXT_PRIMARY)
        c.setFont("Helvetica-Bold", 20)
        c.drawString(MARGIN, y, prop["address"][:60])
        bits = [b for b in (prop.get("city", ""), f"{prop['county']} County" if prop.get("county") else "") if b]
    else:
        c.setFillColor(HexColor("#b91c1c"))
        c.setFont("Helvetica-Bold", 20)
        c.drawString(MARGIN, y, "Property address not filled in")
        bits = ["Section 2A is blank"]
    bits.append(f"Checked {generated_at.strftime('%b %d, %Y')}")
    if files_line:
        bits.append(files_line)
    y -= 0.25 * inch
    c.setFillColor(TEXT_MUTED)
    c.setFont("Helvetica", 9)
    c.drawString(MARGIN, y, "  ·  ".join(bits))
    y -= 0.16 * inch
    c.setFillColor(TEXT_DIM)
    c.setFont("Helvetica", 7.5)
    c.drawString(MARGIN, y, f"File: {filename}")
    y -= 0.32 * inch

    nb = sum(1 for i in issues if i.get("severity") == "blocker")
    nw = len(issues) - nb
    if not issues:
        label, color, bg = "Clear — ready to send", COMPLETE_COLOR, COMPLETE_BG
    elif nb:
        label, color, bg = "Not ready — fix before this goes to title", BLOCKER_COLOR, BLOCKER_BG
    else:
        label, color, bg = f"{nw} item{'s' if nw != 1 else ''} to review", WARNING_COLOR, WARNING_BG
    box_h = 0.62 * inch if issues else 0.45 * inch
    _draw_rounded_rect(c, MARGIN, y - box_h, content_w, box_h, r=7, fill_color=bg)
    c.setFillColor(color)
    c.rect(MARGIN, y - box_h, 4, box_h, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 11.5)
    c.drawString(MARGIN + 0.22 * inch, y - 0.24 * inch, label.upper())
    if issues:
        x = MARGIN + 0.22 * inch
        c.setFont("Helvetica-Bold", 9)
        if nb:
            t = f"{nb} must fix"
            c.setFillColor(BLOCKER_COLOR)
            c.drawString(x, y - 0.46 * inch, t)
            x += c.stringWidth(t, "Helvetica-Bold", 9) + 14
        if nw:
            c.setFillColor(WARNING_COLOR)
            c.drawString(x, y - 0.46 * inch, f"{nw} to review")
    return y - box_h - 0.3 * inch


def _wrap(c, text, font, size, width):
    return _wrap_text(c, text, font, size, width) or [""]


def _section_h(c, content_w):
    return 0.36 * inch


def _draw_section(c, heading, count, y, width):
    c.setFillColor(GREEN)
    c.setFont("Helvetica-Bold", 8)
    t = heading.upper()
    c.drawString(MARGIN, y, t)
    c.setFillColor(TEXT_DIM)
    c.drawString(MARGIN + c.stringWidth(t, "Helvetica-Bold", 8) + 5, y, f"({count})")
    return y - 0.22 * inch


def _issue_h(c, issue, content_w):
    lines = _wrap(c, issue.get("message", ""), ROW_FONT[0], ROW_FONT[1], content_w)
    return 0.2 * inch + len(lines) * LINE_H + 0.16 * inch


def _draw_issue(c, issue, y, content_w, width):
    blocker = issue.get("severity") == "blocker"
    color, bg = (BLOCKER_COLOR, BLOCKER_BG) if blocker else (WARNING_COLOR, WARNING_BG)
    tag = (issue.get("tag") or issue.get("severity") or "issue").upper()
    tag_w = c.stringWidth(tag, "Helvetica-Bold", 6.5) + 12
    _draw_rounded_rect(c, MARGIN, y - 3, tag_w, 12, r=3, fill_color=bg)
    c.setFillColor(color)
    c.setFont("Helvetica-Bold", 6.5)
    c.drawString(MARGIN + 6, y + 0.5, tag)
    ty = y - 0.2 * inch
    c.setFillColor(TEXT_PRIMARY)
    c.setFont(*ROW_FONT)
    for line in _wrap(c, issue.get("message", ""), ROW_FONT[0], ROW_FONT[1], content_w):
        c.drawString(MARGIN, ty, line)
        ty -= LINE_H
    ty -= 0.02 * inch
    c.setStrokeColor(DIVIDER)
    c.setLineWidth(0.5)
    c.line(MARGIN, ty + 0.06 * inch, width - MARGIN, ty + 0.06 * inch)
    return ty - 0.12 * inch


def _box_lines(c, paragraphs, inner_w, size=8.8):
    return [_wrap(c, p, "Helvetica", size, inner_w) for p in paragraphs]


def _next_h(c, content_w):
    lines = _box_lines(c, _NEXT_STEPS, content_w - 0.4 * inch)
    return 0.42 * inch + sum(len(l) for l in lines) * 0.155 * inch + len(lines) * 0.05 * inch + 0.12 * inch + 0.2 * inch


def _draw_next(c, y, content_w, width):
    h = _next_h(c, content_w) - 0.2 * inch
    _draw_rounded_rect(c, MARGIN, y - h, content_w, h, r=8, fill_color=GREEN_TINT)
    x = MARGIN + 0.2 * inch
    ty = y - 0.26 * inch
    c.setFillColor(GREEN)
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(x, ty, "WHAT TO DO NEXT")
    ty -= 0.22 * inch
    c.setFillColor(TEXT_BODY)
    c.setFont("Helvetica", 8.8)
    for para in _box_lines(c, _NEXT_STEPS, content_w - 0.4 * inch):
        for line in para:
            c.drawString(x, ty, line)
            ty -= 0.155 * inch
        ty -= 0.05 * inch
    return y - h - 0.2 * inch


def _archive_h(c, content_w):
    lines = _wrap(c, _ARCHIVE_TEXT, "Helvetica", 8.8, content_w - 0.4 * inch)
    return 0.46 * inch + len(lines) * 0.155 * inch + 0.14 * inch + 0.2 * inch


def _draw_archive(c, y, content_w, width):
    h = _archive_h(c, content_w) - 0.2 * inch
    _draw_rounded_rect(c, MARGIN, y - h, content_w, h, r=8, fill_color=YELLOW_TINT, stroke_color=YELLOW)
    x = MARGIN + 0.2 * inch
    ty = y - 0.26 * inch
    c.setFillColor(TEXT_PRIMARY)
    c.setFont("Helvetica-Bold", 10.5)
    c.drawString(x, ty, _ARCHIVE_HEADING)
    ty -= 0.21 * inch
    c.setFillColor(YELLOW_TEXT)
    c.setFont("Helvetica", 8.8)
    for line in _wrap(c, _ARCHIVE_TEXT, "Helvetica", 8.8, content_w - 0.4 * inch):
        c.drawString(x, ty, line)
        ty -= 0.155 * inch
    return y - h - 0.2 * inch


def _upsell_h(c, content_w):
    lines = _wrap(c, _UPSELL_TEXT, "Helvetica", 8.8, content_w - 0.4 * inch)
    return 0.48 * inch + len(lines) * 0.155 * inch + 0.48 * inch + 0.2 * inch


def _draw_upsell(c, y, content_w, width):
    h = _upsell_h(c, content_w) - 0.2 * inch
    _draw_rounded_rect(c, MARGIN, y - h, content_w, h, r=8, fill_color=GREEN_DARK)
    x = MARGIN + 0.2 * inch
    ty = y - 0.28 * inch
    c.setFillColor(WHITE)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(x, ty, _UPSELL_HEADING)
    ty -= 0.21 * inch
    c.setFillColor(GREEN_TEXT_LIGHT)
    c.setFont("Helvetica", 8.8)
    for line in _wrap(c, _UPSELL_TEXT, "Helvetica", 8.8, content_w - 0.4 * inch):
        c.drawString(x, ty, line)
        ty -= 0.155 * inch
    ty -= 0.1 * inch
    pill_w = c.stringWidth(_UPSELL_CTA, "Helvetica-Bold", 9) + 26
    pill_h = 0.3 * inch
    _draw_rounded_rect(c, x, ty - pill_h + 0.06 * inch, pill_w, pill_h, r=pill_h / 2, fill_color=YELLOW)
    c.setFillColor(TEXT_PRIMARY)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(x + 13, ty - pill_h / 2 + 0.03 * inch, _UPSELL_CTA)
    c.linkURL(_UPSELL_URL, (x, ty - pill_h + 0.06 * inch, x + pill_w, ty + 0.06 * inch), relative=0, thickness=0)
    return y - h - 0.2 * inch


def generate_tc_report_pdf(filename: str, issues: list, generated_at=None, check_count: int = 0,
                           property: dict = None, files_line: str = "") -> bytes:
    """issues: report-ready list from /v1/tc/check's display_issues
    (severity, tag, message). Older callers that pass raw issues without a
    tag still work -- the severity is shown in its place."""
    generated_at = generated_at or datetime.now()
    streak_note = _streak_note(check_count).strip()
    blockers = [i for i in issues if i.get("severity") == "blocker"]
    warnings = [i for i in issues if i.get("severity") == "warning"]
    width, height = letter
    content_w = width - 2 * MARGIN

    rows = []
    if blockers:
        rows.append(("section", "Fix before this goes to title", len(blockers)))
        rows += [("issue", i) for i in blockers]
    if warnings:
        rows.append(("section", "Worth fixing", len(warnings)))
        rows += [("issue", i) for i in warnings]
    if issues:
        rows.append(("next",))
    rows.append(("archive",))
    rows.append(("upsell",))

    def height_of(c, row):
        kind = row[0]
        if kind == "section":
            return _section_h(c, content_w)
        if kind == "issue":
            return _issue_h(c, row[1], content_w)
        if kind == "next":
            return _next_h(c, content_w)
        if kind == "archive":
            return _archive_h(c, content_w)
        return _upsell_h(c, content_w)

    def render(c, final: bool, page_count: int = 1) -> int:
        page = 1
        y = _title_block(c, width, height, property, files_line, filename, generated_at, issues)
        if not issues:
            c.setFillColor(COMPLETE_COLOR)
            c.setFont("Helvetica", 10)
            c.drawString(MARGIN, y, "Nothing to fix on the fields TC Check verifies — this one is ready.")
            y -= 0.4 * inch
        for row in rows:
            h = height_of(c, row)
            # Keep a section heading with its first row.
            if row[0] == "section":
                nxt = rows[rows.index(row) + 1]
                h += height_of(c, nxt)
            if y - h < BOTTOM_LIMIT:
                if final:
                    _footer(c, width, page, page_count, streak_note)
                c.showPage()
                page += 1
                y = _header(c, width, height, compact=True)
            kind = row[0]
            if kind == "section":
                y = _draw_section(c, row[1], row[2], y, width)
            elif kind == "issue":
                y = _draw_issue(c, row[1], y, content_w, width)
            elif kind == "next":
                y = _draw_next(c, y - 0.08 * inch, content_w, width)
            elif kind == "archive":
                y = _draw_archive(c, y, content_w, width)
            else:
                y = _draw_upsell(c, y, content_w, width)
        if final:
            _footer(c, width, page, page_count, streak_note)
        return page

    pages = render(canvas.Canvas(io.BytesIO(), pagesize=letter), final=False)
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    c.setTitle("TC File Check report" + (f" — {property['address']}" if property and property.get("address") else ""))
    render(c, final=True, page_count=pages)
    c.save()
    return buffer.getvalue()
