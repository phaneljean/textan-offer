"""
cover_page.py - Generate dark/emerald cover page for TREC contracts
Matches the site's design system: #0f172a bg, emerald accents, glass-morphism cards.
"""
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, Color
from datetime import datetime, timedelta
import io

# Dark palette
BG_DARK = HexColor("#0f172a")
BG_ELEVATED = HexColor("#1e293b")
CARD_BG = Color(1, 1, 1, alpha=0.035)
CARD_BORDER = Color(1, 1, 1, alpha=0.07)
ACCENT = HexColor("#10b981")
ACCENT_LIGHT = HexColor("#34d399")
TEXT_PRIMARY = HexColor("#f8fafc")
TEXT_MUTED = HexColor("#94a3b8")
TEXT_DIM = HexColor("#64748b")
AMBER = HexColor("#fbbf24")
AMBER_BG = Color(0.96, 0.62, 0.04, alpha=0.06)
AMBER_BORDER = Color(0.96, 0.62, 0.04, alpha=0.15)
DIVIDER = Color(1, 1, 1, alpha=0.05)


def _draw_bg(c, width, height):
    """Dark gradient background with emerald accent bar."""
    c.setFillColor(BG_DARK)
    c.rect(0, 0, width, height, fill=1, stroke=0)

    c.setFillColor(BG_ELEVATED)
    c.rect(0, 0, width, height * 0.35, fill=1, stroke=0)

    c.setFillColor(ACCENT)
    c.rect(0, height - 4, width, 4, fill=1, stroke=0)

    c.saveState()
    c.setFillColor(Color(0.063, 0.725, 0.506, alpha=0.07))
    c.circle(width - 0.5*inch, height - 0.5*inch, 2*inch, fill=1, stroke=0)
    c.restoreState()


def _draw_rounded_rect(c, x, y, w, h, r=6, fill_color=None, stroke_color=None):
    """Draw a rounded rectangle."""
    p = c.beginPath()
    p.roundRect(x, y, w, h, r)
    p.close()
    if fill_color:
        c.setFillColor(fill_color)
    if stroke_color:
        c.setStrokeColor(stroke_color)
        c.setLineWidth(0.5)
    c.drawPath(p, fill=1 if fill_color else 0, stroke=1 if stroke_color else 0)


def generate_cover_page(parsed: dict, agent: dict) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter

    _draw_bg(c, width, height)

    margin = 0.65 * inch
    content_w = width - 2 * margin
    cx = width / 2

    # === HEADER ===
    y = height - 0.6 * inch
    # Brand
    c.setFillColor(ACCENT)
    c.circle(margin + 12, y - 3, 12, fill=1, stroke=0)
    c.setFillColor(TEXT_PRIMARY)
    c.setFont("Helvetica-Bold", 7)
    c.drawCentredString(margin + 12, y - 6, "TX")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(margin + 30, y - 7, "TxtAnOffer")

    # TREC badge (right side)
    badge_text = "TREC 20-19 + 40-11"
    c.setFont("Helvetica-Bold", 6.5)
    tw = c.stringWidth(badge_text, "Helvetica-Bold", 6.5)
    badge_x = width - margin - tw - 16
    _draw_rounded_rect(c, badge_x - 4, y - 12, tw + 22, 18, r=9,
                       fill_color=Color(0.063, 0.725, 0.506, alpha=0.1),
                       stroke_color=Color(0.063, 0.725, 0.506, alpha=0.2))
    c.setFillColor(ACCENT_LIGHT)
    c.drawString(badge_x + 7, y - 7, badge_text)

    # === TITLE BLOCK ===
    y -= 0.7 * inch
    c.setFillColor(TEXT_PRIMARY)
    c.setFont("Helvetica-Bold", 17)
    c.drawCentredString(cx, y, "Residential Purchase Offer")
    y -= 0.28 * inch
    c.setFillColor(TEXT_MUTED)
    c.setFont("Helvetica", 9)
    c.drawCentredString(cx, y, "Generated Contract Summary — Review Before Signing")

    # === PROPERTY ADDRESS CARD ===
    y -= 0.55 * inch
    card_h = 0.85 * inch
    _draw_rounded_rect(c, margin, y - card_h + 0.15*inch, content_w, card_h, r=8,
                       fill_color=Color(1, 1, 1, alpha=0.035),
                       stroke_color=Color(1, 1, 1, alpha=0.07))

    address = parsed.get('address', '')
    city = parsed.get('city', '')
    county = parsed.get('county', '')
    location_parts = []
    if city:
        location_parts.append(city)
    if county:
        location_parts.append(f"{county} County")
    location_parts.append("TX")
    city_state = ", ".join(location_parts)

    c.setFillColor(TEXT_PRIMARY)
    c.setFont("Helvetica-Bold", 13)
    c.drawCentredString(cx, y - 0.28*inch, address)
    c.setFillColor(TEXT_MUTED)
    c.setFont("Helvetica", 9)
    c.drawCentredString(cx, y - 0.52*inch, city_state)

    y -= card_h + 0.2 * inch

    # === STATS GRID (3 boxes) ===
    price = parsed.get('price', 0)
    down_pct = parsed.get('down_payment_pct', 0)
    down_amt = parsed.get('down_payment_amount', 0)
    loan_amt = parsed.get('loan_amount', 0)

    box_w = (content_w - 0.2*inch) / 3
    box_h = 0.7 * inch
    stats = [
        ("SALES PRICE", f"${price:,}"),
        ("DOWN PAYMENT", f"{down_pct*100:.0f}% (${down_amt:,})"),
        ("LOAN AMOUNT", f"${loan_amt:,}"),
    ]

    for i, (label, value) in enumerate(stats):
        bx = margin + i * (box_w + 0.1*inch)
        _draw_rounded_rect(c, bx, y - box_h, box_w, box_h, r=6,
                           fill_color=Color(1, 1, 1, alpha=0.025),
                           stroke_color=Color(1, 1, 1, alpha=0.05))
        c.setFillColor(TEXT_DIM)
        c.setFont("Helvetica-Bold", 6)
        c.drawCentredString(bx + box_w/2, y - 0.22*inch, label)
        c.setFillColor(ACCENT_LIGHT if i == 0 else TEXT_PRIMARY)
        font_size = 10 if len(value) < 12 else 8.5
        c.setFont("Helvetica-Bold", font_size)
        c.drawCentredString(bx + box_w/2, y - 0.46*inch, value)

    y -= box_h + 0.2 * inch

    # === FINANCIAL BREAKDOWN TABLE ===
    close_days = parsed.get('close_days', 0)
    close_date = (datetime.now() + timedelta(days=close_days)).strftime("%B %d, %Y")
    earnest = parsed.get('earnest_money', 0)
    option = parsed.get('option_fee', 0)

    rows = [
        ("Sales Price", f"${price:,}"),
        (f"Down Payment ({down_pct*100:.0f}%)", f"${down_amt:,}"),
        ("Loan Amount", f"${loan_amt:,}"),
        ("Earnest Money", f"${earnest:,}"),
        ("Option Fee", f"${option:,}"),
        ("Closing Date", close_date),
    ]

    table_h = len(rows) * 0.3 * inch + 0.2 * inch
    _draw_rounded_rect(c, margin, y - table_h, content_w, table_h, r=6,
                       fill_color=Color(1, 1, 1, alpha=0.025),
                       stroke_color=Color(1, 1, 1, alpha=0.05))

    row_y = y - 0.28 * inch
    for i, (label, value) in enumerate(rows):
        is_last = (i == len(rows) - 1)
        c.setFillColor(TEXT_PRIMARY if is_last else TEXT_MUTED)
        c.setFont("Helvetica-Bold" if is_last else "Helvetica", 9)
        c.drawString(margin + 0.2*inch, row_y, label)
        c.setFillColor(ACCENT_LIGHT if is_last else HexColor("#e2e8f0"))
        c.setFont("Helvetica-Bold", 10 if is_last else 9)
        c.drawRightString(width - margin - 0.2*inch, row_y, value)

        if not is_last:
            row_y -= 0.05 * inch
            c.setStrokeColor(Color(1, 1, 1, alpha=0.035))
            c.setLineWidth(0.3)
            c.line(margin + 0.15*inch, row_y, width - margin - 0.15*inch, row_y)
            row_y -= 0.25 * inch
        else:
            row_y -= 0.3 * inch

    y -= table_h + 0.2 * inch

    # === AGENT CARD ===
    agent_name = agent.get('name', '')
    license_num = agent.get('license', '')
    brokerage = agent.get('brokerage', '')
    agent_phone = agent.get('phone', '')

    if agent_name:
        agent_card_h = 0.6 * inch
        _draw_rounded_rect(c, margin, y - agent_card_h, content_w, agent_card_h, r=6,
                           fill_color=Color(1, 1, 1, alpha=0.025),
                           stroke_color=Color(1, 1, 1, alpha=0.05))

        # Avatar circle
        initials = "".join(w[0].upper() for w in agent_name.split()[:2]) if agent_name else "AG"
        av_x = margin + 0.35 * inch
        av_y = y - agent_card_h/2
        c.setFillColor(HexColor("#3b82f6"))
        c.circle(av_x, av_y, 14, fill=1, stroke=0)
        c.setFillColor(TEXT_PRIMARY)
        c.setFont("Helvetica-Bold", 8)
        c.drawCentredString(av_x, av_y - 3, initials)

        # Agent info
        info_x = margin + 0.75 * inch
        c.setFillColor(TEXT_PRIMARY)
        c.setFont("Helvetica-Bold", 9.5)
        c.drawString(info_x, y - 0.24*inch, agent_name)
        meta_parts = []
        if license_num:
            meta_parts.append(f"License #{license_num}")
        if brokerage:
            meta_parts.append(brokerage)
        if agent_phone:
            meta_parts.append(agent_phone)
        c.setFillColor(TEXT_DIM)
        c.setFont("Helvetica", 7.5)
        c.drawString(info_x, y - 0.42*inch, " · ".join(meta_parts))

        y -= agent_card_h + 0.2 * inch

    # === DISCLAIMER ===
    disc_h = 0.7 * inch
    _draw_rounded_rect(c, margin, y - disc_h, content_w, disc_h, r=6,
                       fill_color=AMBER_BG, stroke_color=AMBER_BORDER)

    c.setFillColor(AMBER)
    c.setFont("Helvetica-Bold", 7)
    c.drawString(margin + 0.2*inch, y - 0.2*inch, "IMPORTANT NOTICE")
    c.setFillColor(TEXT_MUTED)
    c.setFont("Helvetica", 7.5)
    disc_text = (
        "This is a summary only. The attached TREC 20-19 and 40-11 are the official "
        "promulgated forms required by the Texas Real Estate Commission. This summary "
        "does not modify or replace any provision of the TREC contract. Agent must "
        "review all pages before signing."
    )
    text_obj = c.beginText(margin + 0.2*inch, y - 0.38*inch)
    text_obj.setFont("Helvetica", 7.5)
    text_obj.setFillColor(TEXT_MUTED)
    words = disc_text.split()
    line = ""
    max_line_w = content_w - 0.4*inch
    for word in words:
        test = line + " " + word if line else word
        if c.stringWidth(test, "Helvetica", 7.5) < max_line_w:
            line = test
        else:
            text_obj.textLine(line)
            line = word
    if line:
        text_obj.textLine(line)
    c.drawText(text_obj)

    y -= disc_h + 0.2 * inch

    # === FOOTER ===
    c.setStrokeColor(Color(1, 1, 1, alpha=0.05))
    c.setLineWidth(0.5)
    c.line(margin, y, width - margin, y)
    y -= 0.25 * inch

    c.setFillColor(TEXT_DIM)
    c.setFont("Helvetica-Bold", 7)
    c.drawString(margin, y, "TREC No. 20-19 (05-04-2026)")
    y -= 0.16 * inch
    c.setFont("Helvetica", 7)
    c.drawString(margin, y, "Third Party Financing Addendum (40-11) attached")
    y -= 0.16 * inch
    c.drawString(margin, y, "Not affiliated with TREC · Operated by Phanel")

    # Draft badge (right side)
    badge_y = y + 0.25 * inch
    _draw_rounded_rect(c, width - margin - 1.3*inch, badge_y, 1.3*inch, 0.22*inch, r=9,
                       fill_color=Color(0.96, 0.62, 0.04, alpha=0.1),
                       stroke_color=Color(0.96, 0.62, 0.04, alpha=0.2))
    c.setFillColor(AMBER)
    c.setFont("Helvetica-Bold", 6.5)
    c.drawCentredString(width - margin - 0.65*inch, badge_y + 0.06*inch, "DRAFT — REVIEW REQUIRED")

    timestamp = datetime.now().strftime("%m/%d/%Y %I:%M %p")
    c.setFillColor(TEXT_DIM)
    c.setFont("Helvetica", 6.5)
    c.drawRightString(width - margin, badge_y - 0.18*inch, f"Generated {timestamp}")

    c.save()
    buffer.seek(0)
    return buffer.getvalue()
