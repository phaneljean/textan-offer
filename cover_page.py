"""
cover_page.py - Generate dark or light/print cover page for TREC contracts
Matches the site's design system: black/charcoal accents, glass-morphism cards.
"""
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, Color
from datetime import datetime, timedelta
import io
import re

# Redrawn from static/logo.svg (viewBox 0 0 64 64) as native PDF vector
# shapes rather than an embedded raster -- reportlab can't render SVG
# directly and no SVG-rasterizing library is installed, so this traces
# the same rounded-square + zigzag path at PDF scale. If the live SVG is
# ever redesigned, update these coordinates to match.
_LOGO_RECT = (2, 2, 60, 60, 15)  # x, y, w, h, rx in SVG units
_LOGO_PATH_SVG_PTS = [  # SVG-space points (y grows downward), from the <path> d=
    (18, 44), (18, 20), (38, 20), (28, 30), (46, 30), (46, 48), (28, 48), (38, 38), (18, 38),
]


def _draw_brand(c, x, y):
    """Draws the real TxtAnOffer logo at (x, y-15)..(x+24, y+9) -- same
    footprint as the circular badge it replaces, so header layout doesn't
    shift. Vector-drawn (see note above), so it never depends on an image
    file being present -- a broken asset must never block real offer
    generation."""
    size = 24
    scale = size / 64

    def pt(sx, sy):
        # Flip SVG's y-down to PDF's y-up, then shift down 15pt so the mark's
        # own footprint is (y-15)..(y+9) as documented -- without this offset
        # the mark sits entirely above y (y..y+24) while the wordmark's
        # baseline is drawn at y-7, leaving the two vertically misaligned:
        # the text ends up hanging below the mark instead of centered beside it.
        return x + sx * scale, (y - 15) + (64 - sy) * scale

    c.saveState()
    rx, ry, rw, rh, rr = _LOGO_RECT
    px, py = pt(rx, ry + rh)  # top-left in SVG becomes bottom-left after the y-flip
    _draw_rounded_rect(c, px, py, rw * scale, rh * scale, r=rr * scale,
                       fill_color=HexColor("#1e293b"), stroke_color=HexColor("#334155"))

    p = c.beginPath()
    start_x, start_y = pt(*_LOGO_PATH_SVG_PTS[0])
    p.moveTo(start_x, start_y)
    for sx, sy in _LOGO_PATH_SVG_PTS[1:]:
        px2, py2 = pt(sx, sy)
        p.lineTo(px2, py2)
    p.close()
    c.setStrokeColor(HexColor("#ffffff"))
    c.setLineWidth(3.4 * scale)
    c.setLineCap(1)   # round
    c.setLineJoin(1)  # round
    c.drawPath(p, fill=0, stroke=1)
    c.restoreState()

# Fixed accent used in both modes (readable on white and on navy alike).
ACCENT_RGB = (0.09, 0.09, 0.09)  # ~#171717, matches the site's black/charcoal accent
ON_ACCENT = HexColor("#ffffff")  # text drawn on top of solid accent/avatar shapes

PALETTES = {
    "dark": {
        "page_bg": HexColor("#0f172a"),
        "elevated_bg": HexColor("#1e293b"),
        "accent": HexColor("#e5e7eb"),
        "accent_light": HexColor("#ffffff"),
        "text_primary": HexColor("#f8fafc"),
        "text_muted": HexColor("#94a3b8"),
        "text_dim": HexColor("#64748b"),
        "text_row_value": HexColor("#e2e8f0"),
        "amber": HexColor("#fbbf24"),
        "amber_bg": Color(0.96, 0.62, 0.04, alpha=0.06),
        "amber_border": Color(0.96, 0.62, 0.04, alpha=0.15),
        "divider": Color(1, 1, 1, alpha=0.05),
        "corner_glow_alpha": 0.07,
        "surface_tint": (1, 1, 1),  # white-tinted glass on a dark page
        "surface_scale": 1.0,
    },
    "light": {
        "page_bg": HexColor("#ffffff"),
        "elevated_bg": None,  # no bottom gradient band when printing
        "accent": HexColor("#171717"),
        "accent_light": HexColor("#000000"),
        "text_primary": HexColor("#111827"),
        "text_muted": HexColor("#4b5563"),
        "text_dim": HexColor("#6b7280"),
        "text_row_value": HexColor("#1f2937"),
        "amber": HexColor("#92400e"),
        "amber_bg": HexColor("#fef3c7"),
        "amber_border": HexColor("#fde68a"),
        "divider": HexColor("#e5e7eb"),
        "corner_glow_alpha": 0.05,
        "surface_tint": (0, 0, 0),  # black-tinted (light gray) card fills on white
        "surface_scale": 0.6,  # black tints read darker than white ones at equal alpha
    },
}


def _fmt_pct(pct: float) -> str:
    """0.03 -> '3%', 0.0606 -> '6.06%' -- never silently rounds off real precision."""
    pct100 = pct * 100
    if pct100 == int(pct100):
        return f"{int(pct100)}%"
    return f"{pct100:.2f}".rstrip('0').rstrip('.') + "%"


def _fmt_phone(phone: str) -> str:
    """'+15622570392' -> '+1 (562) 257-0392'; falls back to the raw string
    for anything that isn't a clean 10/11-digit US number."""
    digits = re.sub(r'\D', '', phone or '')
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    if len(digits) == 10:
        return f"+1 ({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"
    return phone


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


def _draw_bg(c, width, height, pal, mode):
    """Page background with accent bar; dark mode adds a gradient-ish bottom panel."""
    c.setFillColor(pal["page_bg"])
    c.rect(0, 0, width, height, fill=1, stroke=0)

    if mode == "dark" and pal["elevated_bg"] is not None:
        c.setFillColor(pal["elevated_bg"])
        c.rect(0, 0, width, height * 0.35, fill=1, stroke=0)

    c.setFillColor(pal["accent"])
    c.rect(0, height - 4, width, 4, fill=1, stroke=0)

    c.saveState()
    c.setFillColor(Color(*ACCENT_RGB, alpha=pal["corner_glow_alpha"]))
    c.circle(width - 0.5*inch, height - 0.5*inch, 2*inch, fill=1, stroke=0)
    c.restoreState()


def generate_cover_page(parsed: dict, agent: dict, mode: str = "light") -> bytes:
    """mode: 'light' (print-friendly, default -- sits next to white TREC forms)
    or 'dark' (digital/dashboard preview)."""
    pal = PALETTES.get(mode, PALETTES["light"])

    def surface(alpha):
        """Semi-transparent card surface: white-tint on dark bg, gray-tint on light bg."""
        r, g, b = pal["surface_tint"]
        return Color(r, g, b, alpha=alpha * pal["surface_scale"])

    BG_ELEVATED = pal["elevated_bg"]
    ACCENT = pal["accent"]
    ACCENT_LIGHT = pal["accent_light"]
    TEXT_PRIMARY = pal["text_primary"]
    TEXT_MUTED = pal["text_muted"]
    TEXT_DIM = pal["text_dim"]
    TEXT_ROW_VALUE = pal["text_row_value"]
    AMBER = pal["amber"]
    AMBER_BG = pal["amber_bg"]
    AMBER_BORDER = pal["amber_border"]
    DIVIDER = pal["divider"]

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter

    _draw_bg(c, width, height, pal, mode)

    margin = 0.65 * inch
    content_w = width - 2 * margin
    cx = width / 2

    # === HEADER ===
    y = height - 0.6 * inch
    # Brand
    _draw_brand(c, margin, y)
    c.setFillColor(TEXT_PRIMARY)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(margin + 30, y - 7, "TxtAnOffer")

    # TREC badge (right side)
    badge_text = "TREC 20-19 + 40-11"
    c.setFont("Helvetica-Bold", 6.5)
    tw = c.stringWidth(badge_text, "Helvetica-Bold", 6.5)
    badge_x = width - margin - tw - 16
    _draw_rounded_rect(c, badge_x - 4, y - 12, tw + 22, 18, r=9,
                       fill_color=Color(*ACCENT_RGB, alpha=0.1),
                       stroke_color=Color(*ACCENT_RGB, alpha=0.2))
    c.setFillColor(ACCENT_LIGHT if mode == "dark" else HexColor("#000000"))
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
                       fill_color=surface(0.035),
                       stroke_color=surface(0.07))

    address = parsed.get('address', '')
    city = parsed.get('city', '')
    county = parsed.get('county', '')
    zip_code = parsed.get('zip', '')
    location_parts = []
    if city:
        location_parts.append(city)
    if county:
        location_parts.append(f"{county} County")
    location_parts.append(f"TX {zip_code}" if zip_code else "TX")
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
        ("DOWN PAYMENT", f"{_fmt_pct(down_pct)} (${down_amt:,})"),
        ("LOAN AMOUNT", f"${loan_amt:,}"),
    ]

    for i, (label, value) in enumerate(stats):
        bx = margin + i * (box_w + 0.1*inch)
        _draw_rounded_rect(c, bx, y - box_h, box_w, box_h, r=6,
                           fill_color=surface(0.025),
                           stroke_color=surface(0.05))
        c.setFillColor(TEXT_DIM)
        c.setFont("Helvetica-Bold", 6)
        c.drawCentredString(bx + box_w/2, y - 0.22*inch, label)
        c.setFillColor(ACCENT_LIGHT if i == 0 else TEXT_PRIMARY)
        font_size = 10 if len(value) < 12 else 8.5
        c.setFont("Helvetica-Bold", font_size)
        c.drawCentredString(bx + box_w/2, y - 0.46*inch, value)

    y -= box_h + 0.2 * inch

    # === PROPERTY DETAILS ROW (if MLS data available) ===
    bed = parsed.get('bed', 0)
    bath = parsed.get('bath', 0)
    sqft = parsed.get('sqft', 0)
    year_built = parsed.get('year_built', 0)
    if bed or bath or sqft:
        detail_parts = []
        if bed:
            detail_parts.append(f"{bed} Bed")
        if bath:
            detail_parts.append(f"{bath} Bath")
        if sqft:
            detail_parts.append(f"{sqft:,} Sqft")
        if year_built:
            detail_parts.append(f"Built {year_built}")
        detail_text = "  ·  ".join(detail_parts)

        detail_h = 0.35 * inch
        _draw_rounded_rect(c, margin, y - detail_h, content_w, detail_h, r=6,
                           fill_color=surface(0.02),
                           stroke_color=surface(0.04))
        c.setFillColor(TEXT_MUTED)
        c.setFont("Helvetica", 8.5)
        c.drawCentredString(cx, y - 0.22*inch, detail_text)
        y -= detail_h + 0.15 * inch

    # === FINANCIAL BREAKDOWN TABLE ===
    close_days = parsed.get('close_days', 0)
    close_date = (datetime.now() + timedelta(days=close_days)).strftime("%B %d, %Y")
    earnest = parsed.get('earnest_money', 0)
    option = parsed.get('option_fee', 0)

    rows = [
        ("Sales Price", f"${price:,}"),
        (f"Down Payment ({_fmt_pct(down_pct)})", f"${down_amt:,}"),
        ("Loan Amount", f"${loan_amt:,}"),
        ("Earnest Money", f"${earnest:,}"),
        ("Option Fee", f"${option:,}"),
        ("Closing Date", close_date),
    ]

    table_h = len(rows) * 0.3 * inch + 0.2 * inch
    _draw_rounded_rect(c, margin, y - table_h, content_w, table_h, r=6,
                       fill_color=surface(0.025),
                       stroke_color=surface(0.05))

    row_y = y - 0.28 * inch
    for i, (label, value) in enumerate(rows):
        is_last = (i == len(rows) - 1)
        c.setFillColor(TEXT_PRIMARY if is_last else TEXT_MUTED)
        c.setFont("Helvetica-Bold" if is_last else "Helvetica", 9)
        c.drawString(margin + 0.2*inch, row_y, label)
        c.setFillColor(ACCENT_LIGHT if is_last else TEXT_ROW_VALUE)
        c.setFont("Helvetica-Bold", 10 if is_last else 9)
        c.drawRightString(width - margin - 0.2*inch, row_y, value)

        if not is_last:
            row_y -= 0.05 * inch
            c.setStrokeColor(DIVIDER)
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
                           fill_color=surface(0.025),
                           stroke_color=surface(0.05))

        # Avatar circle
        initials = "".join(w[0].upper() for w in agent_name.split()[:2]) if agent_name else "AG"
        av_x = margin + 0.35 * inch
        av_y = y - agent_card_h/2
        c.setFillColor(HexColor("#3b82f6"))
        c.circle(av_x, av_y, 14, fill=1, stroke=0)
        c.setFillColor(ON_ACCENT)
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
            meta_parts.append(_fmt_phone(agent_phone))
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
    c.setStrokeColor(DIVIDER)
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
    c.drawString(margin, y, "Seller's Disclosure re: Groundwater/Surface Water (61-0) attached")
    y -= 0.16 * inch
    c.drawString(margin, y, "Not affiliated with TREC")

    # Draft badge (right side)
    badge_y = y + 0.25 * inch
    _draw_rounded_rect(c, width - margin - 1.3*inch, badge_y, 1.3*inch, 0.22*inch, r=9,
                       fill_color=AMBER_BG,
                       stroke_color=AMBER_BORDER)
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


# Blue accent for the certification page only -- spec calls for a distinct
# color from the cover page's teal so agents can tell the two apart at a
# glance when flipping through a printed packet.
BLUE_RGB = (0.231, 0.510, 0.965)  # #3b82f6
CERT_BLUE = {
    "dark": {"accent": HexColor("#3b82f6"), "accent_light": HexColor("#60a5fa")},
    "light": {"accent": HexColor("#3b82f6"), "accent_light": HexColor("#2563eb")},
}

REVIEW_CHECKLIST = [
    ("Property Address Verified",
     "I have confirmed the legal description and address match the intended property."),
    ("Sales Price & Financial Terms",
     "I have verified the sales price, down payment, loan amount, earnest money, and option fee."),
    ("Dates & Deadlines",
     "I have confirmed the closing date and all contractual deadlines."),
    ("Buyer & Seller Information",
     "I have filled in or verified all party names and contact information."),
    ("Special Provisions Reviewed",
     "I have read all special provisions and additional clauses, if any."),
]


def _wrap_text(c, text, font, size, max_width):
    """Greedy word-wrap: returns a list of lines that each fit max_width."""
    words = text.split()
    lines = []
    line = ""
    for word in words:
        test = line + " " + word if line else word
        if c.stringWidth(test, font, size) < max_width:
            line = test
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def generate_certification_page(parsed: dict, agent: dict, mode: str = "light") -> bytes:
    """Final-page agent certification + review checklist (spec Part 3).
    Blue accent distinguishes it from the teal cover page. Checkboxes and the
    signature line are drawn empty -- this is a printed page the agent signs
    by hand, not something this app fills in on their behalf."""
    pal = PALETTES.get(mode, PALETTES["light"])
    blue = CERT_BLUE.get(mode, CERT_BLUE["light"])

    def surface(alpha):
        r, g, b = pal["surface_tint"]
        return Color(r, g, b, alpha=alpha * pal["surface_scale"])

    ACCENT = blue["accent"]
    ACCENT_LIGHT = blue["accent_light"]
    TEXT_PRIMARY = pal["text_primary"]
    TEXT_MUTED = pal["text_muted"]
    TEXT_DIM = pal["text_dim"]
    DIVIDER = pal["divider"]

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter

    # Background + top accent bar (blue, not the shared teal _draw_bg helper)
    c.setFillColor(pal["page_bg"])
    c.rect(0, 0, width, height, fill=1, stroke=0)
    if mode == "dark" and pal["elevated_bg"] is not None:
        c.setFillColor(pal["elevated_bg"])
        c.rect(0, 0, width, height * 0.35, fill=1, stroke=0)
    c.setFillColor(ACCENT)
    c.rect(0, height - 4, width, 4, fill=1, stroke=0)
    c.saveState()
    c.setFillColor(Color(*BLUE_RGB, alpha=pal["corner_glow_alpha"]))
    c.circle(width - 0.5*inch, height - 0.5*inch, 2*inch, fill=1, stroke=0)
    c.restoreState()

    margin = 0.65 * inch
    content_w = width - 2 * margin
    cx = width / 2

    # === HEADER (brand) ===
    y = height - 0.6 * inch
    _draw_brand(c, margin, y)
    c.setFillColor(TEXT_PRIMARY)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(margin + 30, y - 7, "TxtAnOffer")

    # === TITLE BLOCK ===
    y -= 0.7 * inch
    c.setFillColor(TEXT_PRIMARY)
    c.setFont("Helvetica-Bold", 17)
    c.drawCentredString(cx, y, "Agent Certification")

    full_addr = parsed.get("full_address") or ", ".join(
        p for p in [parsed.get("address", ""),
                    parsed.get("city", ""),
                    f"TX {parsed.get('zip', '')}".strip()] if p
    )
    y -= 0.28 * inch
    c.setFillColor(TEXT_MUTED)
    c.setFont("Helvetica", 9)
    c.drawCentredString(cx, y, full_addr)

    # === CERTIFICATION STATEMENT ===
    y -= 0.45 * inch
    stmt_text = (
        "By signing below, I confirm that I have personally reviewed all fields "
        "in the attached TREC 20-19 and 40-11 forms, and that they accurately "
        "reflect the terms of this offer as communicated by the buyer."
    )
    stmt_lines = _wrap_text(c, stmt_text, "Helvetica", 9, content_w - 0.4*inch)
    stmt_h = len(stmt_lines) * 0.19 * inch + 0.25 * inch
    _draw_rounded_rect(c, margin, y - stmt_h, content_w, stmt_h, r=6,
                       fill_color=surface(0.03), stroke_color=surface(0.06))
    text_obj = c.beginText(margin + 0.2*inch, y - 0.24*inch)
    text_obj.setFont("Helvetica", 9)
    text_obj.setFillColor(TEXT_PRIMARY)
    text_obj.setLeading(0.19 * inch)
    for line in stmt_lines:
        text_obj.textLine(line)
    c.drawText(text_obj)
    y -= stmt_h + 0.3 * inch

    # === REVIEW CHECKLIST ===
    c.setFillColor(TEXT_DIM)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(margin, y, "REVIEW CHECKLIST")
    y -= 0.06 * inch
    c.setStrokeColor(DIVIDER)
    c.setLineWidth(0.5)
    c.line(margin, y, width - margin, y)
    y -= 0.28 * inch

    box_size = 12
    for title, desc in REVIEW_CHECKLIST:
        desc_lines = _wrap_text(c, desc, "Helvetica", 8, content_w - 0.5*inch)

        # Empty checkbox square
        c.setStrokeColor(HexColor("#d1d5db"))
        c.setLineWidth(1)
        c.rect(margin, y - box_size + 2, box_size, box_size, fill=0, stroke=1)

        c.setFillColor(TEXT_PRIMARY)
        c.setFont("Helvetica-Bold", 9.5)
        c.drawString(margin + 0.28*inch, y, title)

        desc_y = y - 0.16 * inch
        c.setFillColor(TEXT_MUTED)
        c.setFont("Helvetica", 8)
        for line in desc_lines:
            c.drawString(margin + 0.28*inch, desc_y, line)
            desc_y -= 0.14 * inch

        y = desc_y - 0.12 * inch

    y -= 0.1 * inch
    c.setStrokeColor(DIVIDER)
    c.setLineWidth(0.5)
    c.line(margin, y, width - margin, y)
    y -= 0.35 * inch

    # === AGENT ACKNOWLEDGMENT ===
    c.setFillColor(TEXT_DIM)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(margin, y, "AGENT ACKNOWLEDGMENT")
    y -= 0.06 * inch
    c.setStrokeColor(DIVIDER)
    c.setLineWidth(0.5)
    c.line(margin, y, width - margin, y)
    y -= 0.32 * inch

    agent_name = agent.get("name", "")
    license_num = agent.get("license", "")

    c.setFillColor(TEXT_MUTED)
    c.setFont("Helvetica", 8)
    c.drawString(margin, y, "Agent Name")
    c.drawString(cx + 0.2*inch, y, "TREC #")
    y -= 0.2 * inch
    c.setFillColor(TEXT_PRIMARY)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(margin, y, agent_name or "_" * 30)
    c.drawString(cx + 0.2*inch, y, license_num or "_" * 12)
    y -= 0.45 * inch

    sig_line_color = HexColor("#111827") if mode == "light" else HexColor("#f9fafb")
    c.setStrokeColor(sig_line_color)
    c.setLineWidth(0.75)
    sig_w = content_w * 0.6
    c.line(margin, y, margin + sig_w, y)
    date_x = margin + sig_w + 0.3 * inch
    c.line(date_x, y, width - margin, y)

    y -= 0.16 * inch
    c.setFillColor(TEXT_DIM)
    c.setFont("Helvetica", 7.5)
    c.drawString(margin, y, "Signature")
    c.drawString(date_x, y, "Date")

    y -= 0.5 * inch

    # === DISCLAIMER ===
    disc_text = (
        "This certification is generated by TxtAnOffer as a review aid for the "
        "agent. It is not a substitute for the agent's independent professional "
        "judgment and does not modify or replace any provision of the attached "
        "TREC contract."
    )
    disc_lines = _wrap_text(c, disc_text, "Helvetica", 7.5, content_w * 0.85)
    c.setFillColor(TEXT_DIM)
    c.setFont("Helvetica", 7.5)
    for line in disc_lines:
        c.drawCentredString(cx, y, line)
        y -= 0.14 * inch

    # === FOOTER ===
    footer_y = 0.55 * inch
    c.setStrokeColor(DIVIDER)
    c.setLineWidth(0.5)
    c.line(margin, footer_y + 0.15*inch, width - margin, footer_y + 0.15*inch)
    c.setFillColor(TEXT_DIM)
    c.setFont("Helvetica", 7)
    c.drawCentredString(cx, footer_y, "Generated by TxtAnOffer · txtanoffer.com · Not affiliated with TREC")

    c.save()
    buffer.seek(0)
    return buffer.getvalue()
