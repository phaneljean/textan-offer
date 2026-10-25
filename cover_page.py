"""
cover_page.py - Generate dark or light/print cover page for TREC contracts
Matches txtanoffer.com: dark-green brand band, gold accent, green key values.
"""
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, Color
from datetime import datetime, timedelta
from deadlines import offer_closing_date
import io
import os
import re

# Current brand: the same "txtanoffer" wordmark PNG the site nav uses
# (static/logo-wordmark.png, 708x120). Replaced the old rounded-square +
# zigzag mark (static/logo.svg) on 2026-10-12 -- this cover is page 1 of
# every offer PDF, and the /review page an agent opens from the SMS reply
# shows it as its preview image, so the old mark was the first thing they saw.
_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_WORDMARK = {"light": os.path.join(_STATIC, "logo-wordmark.png"),
             "dark": os.path.join(_STATIC, "logo-wordmark-white.png")}

# Fixed accent used in both modes (readable on white and on navy alike).
ACCENT_RGB = (0.09, 0.09, 0.09)  # ~#171717, matches the site's black/charcoal accent

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


# Site brand colors (txtanoffer.com hero/nav/CTAs) -- the cover and the
# certification page share these so the packet reads as one product with the
# website: dark-green band + white wordmark, gold accent, green key values.
BRAND = {
    "green": HexColor("#0a3f3a"),
    "green_mid": HexColor("#0b5d52"),
    "green_tint": HexColor("#eef5f3"),
    "gold": HexColor("#f5c242"),
    "gold_tint": HexColor("#fdf6e3"),
    "gold_border": HexColor("#f1d48a"),
    "ink": HexColor("#0f1f2f"),
    "muted": HexColor("#5a6b7a"),
    "dim": HexColor("#8a9aa9"),
    "line": HexColor("#e3e8ec"),
    "white": HexColor("#ffffff"),
}

_FINANCING_LABELS = {
    "conventional": "Conventional", "fha": "FHA", "va": "VA", "usda": "USDA",
    "texas_veterans": "Texas Veterans", "reverse_mortgage": "Reverse Mortgage",
    "cash": "Cash",
}


def _fmt_long_date(d) -> str:
    """'November 2, 2026' -- no zero-padded day, matching how a person writes it."""
    return f"{d:%B} {d.day}, {d.year}"


def packet_forms(parsed: dict) -> list:
    """The forms actually attached to this offer's PDF, in packet order.
    Must stay in sync with generate_offer_pdf() in pdf_filler.py: the 40-11
    only rides along when there's a loan, the 36-11 only when the text said
    HOA. The cover and certification pages both list exactly these, so a cash
    offer never claims a financing addendum that isn't there."""
    forms = [("TREC 20-19", "One to Four Family Residential Contract (Resale)")]
    if (parsed.get("loan_amount") or 0) > 0:
        forms.append(("TREC 40-11", "Third Party Financing Addendum"))
    if parsed.get("has_hoa"):
        forms.append(("TREC 36-11", "Addendum for Mandatory Owners' Association Membership"))
    forms.append(("TREC 61-0", "Seller's Disclosure re: Groundwater/Surface Water"))
    forms.append(("IABS 1-2", "Information About Brokerage Services"))
    return forms


def _draw_brand_band(c, width, height, right_label=None):
    """Full-width dark-green header band with the white wordmark, a gold
    accent rule underneath, and an optional gold pill on the right."""
    band_h = 64
    c.setFillColor(BRAND["green"])
    c.rect(0, height - band_h, width, band_h, fill=1, stroke=0)
    c.setFillColor(BRAND["gold"])
    c.rect(0, height - band_h - 3, width, 3, fill=1, stroke=0)

    margin = 0.65 * inch
    mid = height - band_h / 2
    logo_h = 15
    logo_w = logo_h * 708 / 120
    try:
        c.drawImage(_WORDMARK["dark"], margin, mid - logo_h / 2, logo_w, logo_h, mask="auto")
    except Exception:
        c.setFillColor(BRAND["white"])
        c.setFont("Helvetica-Bold", 14)
        c.drawString(margin, mid - 5, "txtanoffer")

    if right_label:
        c.setFont("Helvetica-Bold", 7)
        tw = c.stringWidth(right_label, "Helvetica-Bold", 7)
        pw, ph = tw + 20, 18
        px = width - margin - pw
        _draw_rounded_rect(c, px, mid - ph / 2, pw, ph, r=9, fill_color=BRAND["gold"])
        c.setFillColor(BRAND["green"])
        c.drawString(px + 10, mid - 2.5, right_label)
    return height - band_h - 3


def _draw_kicker(c, x, y, text, color=None):
    c.setFillColor(color or BRAND["green_mid"])
    c.setFont("Helvetica-Bold", 7)
    c.drawString(x, y, text.upper())


def _draw_page_footer(c, width, left, right):
    margin = 0.65 * inch
    fy = 0.5 * inch
    c.setStrokeColor(BRAND["line"])
    c.setLineWidth(0.5)
    c.line(margin, fy + 12, width - margin, fy + 12)
    c.setFillColor(BRAND["dim"])
    c.setFont("Helvetica", 7)
    c.drawString(margin, fy, left)
    c.drawRightString(width - margin, fy, right)


def generate_cover_page(parsed: dict, agent: dict, mode: str = "light") -> bytes:
    """Page 1 of every offer PDF, and the preview image on the /review page
    the SMS reply links to. Print-first (it sits in front of white TREC
    forms), styled to match txtanoffer.com. `mode` is kept for callers;
    only the light/print layout exists."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    margin = 0.65 * inch
    content_w = width - 2 * margin
    now = datetime.now()

    c.setFillColor(BRAND["white"])
    c.rect(0, 0, width, height, fill=1, stroke=0)
    y = _draw_brand_band(c, width, height, "DRAFT — REVIEW BEFORE SIGNING")

    # === TITLE ===
    y -= 0.5 * inch
    _draw_kicker(c, margin, y, "Generated contract summary")
    y -= 0.34 * inch
    c.setFillColor(BRAND["ink"])
    c.setFont("Helvetica-Bold", 22)
    c.drawString(margin, y, "Residential Purchase Offer")
    y -= 0.24 * inch
    c.setFillColor(BRAND["muted"])
    c.setFont("Helvetica", 9)
    c.drawString(margin, y, f"Prepared {_fmt_long_date(now)}  ·  Review every page before signing or presenting")

    # === PROPERTY ===
    address = parsed.get('address', '') or ''
    location_parts = []
    if parsed.get('city'):
        location_parts.append(parsed['city'])
    if parsed.get('county'):
        location_parts.append(f"{parsed['county']} County")
    location_parts.append(f"TX {parsed['zip']}" if parsed.get('zip') else "TX")
    location = ", ".join(location_parts)

    y -= 0.32 * inch
    card_h = 0.95 * inch
    _draw_rounded_rect(c, margin, y - card_h, content_w, card_h, r=8, fill_color=BRAND["green_tint"])
    c.setFillColor(BRAND["green"])
    c.rect(margin, y - card_h + 8, 3, card_h - 16, fill=1, stroke=0)
    _draw_kicker(c, margin + 0.25 * inch, y - 0.26 * inch, "Property")
    c.setFillColor(BRAND["ink"])
    font_size = 16
    while font_size > 11 and c.stringWidth(address, "Helvetica-Bold", font_size) > content_w - 0.5 * inch:
        font_size -= 0.5
    c.setFont("Helvetica-Bold", font_size)
    c.drawString(margin + 0.25 * inch, y - 0.52 * inch, address)
    c.setFillColor(BRAND["muted"])
    c.setFont("Helvetica", 9.5)
    c.drawString(margin + 0.25 * inch, y - 0.72 * inch, location)

    # Property details from the listing lookup, when there are any
    details = []
    if parsed.get('bed'):
        details.append(f"{parsed['bed']} Bed")
    if parsed.get('bath'):
        details.append(f"{parsed['bath']} Bath")
    if parsed.get('sqft'):
        details.append(f"{parsed['sqft']:,} Sqft")
    if parsed.get('year_built'):
        details.append(f"Built {parsed['year_built']}")
    if details and (parsed.get('bed') or parsed.get('bath') or parsed.get('sqft')):
        c.setFont("Helvetica", 8.5)
        c.drawRightString(width - margin - 0.25 * inch, y - 0.72 * inch, "  ·  ".join(details))
    y -= card_h + 0.22 * inch

    # === KEY TERMS (3 tiles) ===
    price = parsed.get('price', 0) or 0
    down_pct = parsed.get('down_payment_pct', 0) or 0
    down_amt = parsed.get('down_payment_amount', 0) or 0
    loan_amt = parsed.get('loan_amount', 0) or 0
    close_days = parsed.get('close_days', 0) or 0
    # Same date as the 9A overlay (deadlines.offer_closing_date), not now + N.
    close_date = _fmt_long_date(offer_closing_date(close_days, parsed.get("created_at"))) if close_days else "—"

    tiles = [
        ("Sales price", f"${price:,}", BRAND["green"]),
        ("Down payment", f"${down_amt:,}  ({_fmt_pct(down_pct)})", BRAND["ink"]) if loan_amt > 0
        else ("Payment", "All cash", BRAND["ink"]),
        ("Closing on or before", close_date, BRAND["ink"]),
    ]
    gap = 0.12 * inch
    tile_w = (content_w - 2 * gap) / 3
    tile_h = 0.78 * inch
    for i, (label, value, color) in enumerate(tiles):
        tx = margin + i * (tile_w + gap)
        _draw_rounded_rect(c, tx, y - tile_h, tile_w, tile_h, r=7,
                           fill_color=BRAND["white"], stroke_color=BRAND["line"])
        c.setFillColor(BRAND["dim"])
        c.setFont("Helvetica-Bold", 6.5)
        c.drawString(tx + 0.17 * inch, y - 0.25 * inch, label.upper())
        size = 15
        while size > 9 and c.stringWidth(value, "Helvetica-Bold", size) > tile_w - 0.34 * inch:
            size -= 0.5
        c.setFillColor(color)
        c.setFont("Helvetica-Bold", size)
        c.drawString(tx + 0.17 * inch, y - 0.55 * inch, value)
    y -= tile_h + 0.28 * inch

    # === OFFER TERMS ===
    ftype = parsed.get('financing_type')
    if loan_amt > 0:
        financing = _FINANCING_LABELS.get(ftype or "conventional", (ftype or "").title())
        if not ftype:
            financing += " (default — confirm)"
    else:
        financing = "Cash"
    rows = [("Financing", financing)]
    if loan_amt > 0:
        rows.append(("Loan amount", f"${loan_amt:,}"))
    rows.append(("Earnest money", f"${parsed.get('earnest_money', 0) or 0:,}"))
    rows.append(("Option fee", f"${parsed.get('option_fee', 0) or 0:,}"))
    if parsed.get('inspection_days') is not None:
        rows.append(("Option period", f"{parsed['inspection_days']} days"))
    if close_days:
        rows.append(("Days to close", f"{close_days} days from today"))

    _draw_kicker(c, margin, y, "Offer terms")
    y -= 0.12 * inch
    c.setStrokeColor(BRAND["ink"])
    c.setLineWidth(0.8)
    c.line(margin, y, width - margin, y)
    for label, value in rows:
        y -= 0.24 * inch
        c.setFillColor(BRAND["muted"])
        c.setFont("Helvetica", 9.5)
        c.drawString(margin, y, label)
        c.setFillColor(BRAND["ink"])
        c.setFont("Helvetica-Bold", 9.5)
        c.drawRightString(width - margin, y, value)
        y -= 0.09 * inch
        c.setStrokeColor(BRAND["line"])
        c.setLineWidth(0.5)
        c.line(margin, y, width - margin, y)
    y -= 0.32 * inch

    # === AGENT ===
    agent_name = agent.get('name', '')
    if agent_name:
        meta = []
        if agent.get('license'):
            meta.append(f"TREC #{agent['license']}")
        if agent.get('brokerage'):
            meta.append(agent['brokerage'])
        if agent.get('phone'):
            meta.append(_fmt_phone(agent['phone']))
        _draw_kicker(c, margin, y, "Buyer's agent")
        y -= 0.24 * inch
        initials = "".join(w[0].upper() for w in agent_name.split()[:2])
        c.setFillColor(BRAND["green"])
        c.circle(margin + 13, y - 4, 13, fill=1, stroke=0)
        c.setFillColor(BRAND["white"])
        c.setFont("Helvetica-Bold", 8)
        c.drawCentredString(margin + 13, y - 7, initials)
        c.setFillColor(BRAND["ink"])
        c.setFont("Helvetica-Bold", 10)
        c.drawString(margin + 36, y, agent_name)
        c.setFillColor(BRAND["muted"])
        c.setFont("Helvetica", 8)
        c.drawString(margin + 36, y - 13, "  ·  ".join(meta))
        y -= 0.44 * inch

    # === IN THIS PACKET ===
    forms = packet_forms(parsed) + [("Final page", "Agent Certification & review checklist")]
    _draw_kicker(c, margin, y, "In this packet")
    y -= 0.08 * inch
    for code, name in forms:
        y -= 0.2 * inch
        c.setStrokeColor(BRAND["green_mid"])
        c.setLineWidth(1.3)
        p = c.beginPath()
        p.moveTo(margin + 1, y + 3)
        p.lineTo(margin + 4, y)
        p.lineTo(margin + 9, y + 6)
        c.drawPath(p, stroke=1, fill=0)
        c.setFillColor(BRAND["ink"])
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(margin + 16, y, code)
        c.setFillColor(BRAND["muted"])
        c.setFont("Helvetica", 8.5)
        c.drawString(margin + 16 + 62, y, name)
    y -= 0.26 * inch

    # === NOTICE ===
    notice = ("This page is a summary for review only. The attached TREC forms are the "
              "contract; this summary does not modify or replace any of their terms. The "
              "agent must review and complete every page before signing or presenting.")
    lines = _wrap_text(c, notice, "Helvetica", 8, content_w - 0.4 * inch)
    box_h = 0.36 * inch + len(lines) * 0.15 * inch
    _draw_rounded_rect(c, margin, y - box_h, content_w, box_h, r=7,
                       fill_color=BRAND["gold_tint"], stroke_color=BRAND["gold_border"])
    _draw_kicker(c, margin + 0.2 * inch, y - 0.22 * inch, "Before you sign", BRAND["green"])
    ty = y - 0.4 * inch
    c.setFillColor(BRAND["ink"])
    c.setFont("Helvetica", 8)
    for line in lines:
        c.drawString(margin + 0.2 * inch, ty, line)
        ty -= 0.15 * inch

    _draw_page_footer(c, width, "txtanoffer.com  ·  Not affiliated with TREC",
                      f"TREC 20-19 (05-04-2026)  ·  Generated {now:%m/%d/%Y %I:%M %p}")

    c.save()
    buffer.seek(0)
    return buffer.getvalue()


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
    Shares the cover page's brand band; its gold pill names the page. Checkboxes and the
    signature line are drawn empty -- this is a printed page the agent signs
    by hand, not something this app fills in on their behalf."""
    pal = PALETTES.get(mode, PALETTES["light"])

    def surface(alpha):
        r, g, b = pal["surface_tint"]
        return Color(r, g, b, alpha=alpha * pal["surface_scale"])

    TEXT_PRIMARY = pal["text_primary"]
    TEXT_MUTED = pal["text_muted"]
    TEXT_DIM = pal["text_dim"]
    DIVIDER = pal["divider"]

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter

    # Same brand band as the cover page; the gold pill tells the two apart
    # when flipping through a printed packet.
    c.setFillColor(pal["page_bg"])
    c.rect(0, 0, width, height, fill=1, stroke=0)
    _draw_brand_band(c, width, height, "AGENT CERTIFICATION")

    margin = 0.65 * inch
    content_w = width - 2 * margin
    cx = width / 2

    # === TITLE BLOCK ===
    y = height - 67 - 0.62 * inch
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
    codes = [code for code, _ in packet_forms(parsed)]
    form_list = ", ".join(codes[:-1]) + " and " + codes[-1]
    stmt_text = (
        "By signing below, I confirm that I have personally reviewed all fields "
        f"in the attached {form_list} forms, and that they accurately "
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
    c.setFillColor(BRAND["green_mid"])
    c.setFont("Helvetica-Bold", 8)
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
    c.setFillColor(BRAND["green_mid"])
    c.setFont("Helvetica-Bold", 8)
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

    _draw_page_footer(c, width, "txtanoffer.com  \u00b7  Not affiliated with TREC",
                      "Agent Certification  \u00b7  Final page")

    c.save()
    buffer.seek(0)
    return buffer.getvalue()


# Amber accent, distinct from the cover page's teal and the certification
# page's blue -- and deliberately reads as "sponsored," not as another
# first-party TxtAnOffer page, so a reader never mistakes paid placement
# for the app's own branding.
SPONSOR_PALETTE = dict(PALETTES["light"])
SPONSOR_PALETTE["accent"] = HexColor("#b45309")
SPONSOR_RGB = (0.706, 0.325, 0.035)  # ~#b45309, for the corner glow


def generate_sponsor_page(sponsor: dict, parsed: dict, mode: str = "light") -> bytes:
    """Paid title-company/lender placement page, appended as the LAST page
    of the packet (see pdf_filler.py) so it can never shift the fixed
    page-index overlays that target the TREC form and addenda by absolute
    position. This is its own separate document, same as the cover and
    certification pages -- never drawn onto an actual TREC page.

    Kept deliberately plain (name, tagline, contact info) rather than an
    uploaded logo image: no missing-asset risk, matches this module's
    existing vector-only approach, and keeps a sponsor's page from ever
    silently failing to render.

    The disclaimer at the bottom is not boilerplate -- title company
    placement in a real estate transaction sits near RESPA Section 8's
    anti-kickback rules if it could be read as a required or preferential
    referral. This page must always read as optional advertising, never as
    a requirement or an endorsement by TxtAnOffer, the buyer, or the seller."""
    pal = SPONSOR_PALETTE
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter

    _draw_bg(c, width, height, pal, "light")
    c.saveState()
    c.setFillColor(Color(*SPONSOR_RGB, alpha=0.06))
    c.circle(width - 0.5*inch, height - 0.5*inch, 2*inch, fill=1, stroke=0)
    c.restoreState()

    margin = 0.65 * inch
    content_w = width - 2 * margin
    cx = width / 2
    y = height - 1.3 * inch

    county = (parsed.get("county") or "").strip()
    kicker = f"Preferred Title Partner — {county} County, TX" if county else "Preferred Title Partner"
    c.setFillColor(pal["accent"])
    c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(cx, y, kicker.upper())
    y -= 0.5 * inch

    c.setFillColor(pal["text_primary"])
    c.setFont("Helvetica-Bold", 26)
    for line in _wrap_text(c, sponsor.get("name", ""), "Helvetica-Bold", 26, content_w):
        c.drawCentredString(cx, y, line)
        y -= 0.42 * inch

    tagline = (sponsor.get("tagline") or "").strip()
    if tagline:
        y -= 0.05 * inch
        c.setFillColor(pal["text_muted"])
        c.setFont("Helvetica", 11)
        for line in _wrap_text(c, tagline, "Helvetica", 11, content_w * 0.8):
            c.drawCentredString(cx, y, line)
            y -= 0.22 * inch

    y -= 0.35 * inch
    contact_parts = [p for p in (sponsor.get("contact_phone"), sponsor.get("contact_email")) if p]
    if contact_parts:
        contact_text = "   ·   ".join(contact_parts)
        card_h = 0.5 * inch
        _draw_rounded_rect(c, margin + content_w*0.15, y - card_h + 0.15*inch, content_w*0.7, card_h, r=8,
                           fill_color=HexColor("#fef3ea"), stroke_color=HexColor("#fde3cc"))
        c.setFillColor(pal["accent"])
        c.setFont("Helvetica-Bold", 10)
        c.drawCentredString(cx, y - 0.12*inch, contact_text)
        y -= card_h + 0.15*inch

    address = parsed.get("address", "")
    if address:
        y -= 0.3 * inch
        c.setFillColor(pal["text_dim"])
        c.setFont("Helvetica", 8.5)
        c.drawCentredString(cx, y, f"Regarding: {address}")

    # === DISCLAIMER (see docstring) ===
    disc_y = 1.05 * inch
    disc_text = (
        "This page is a paid sponsor placement and is shown because the property is in "
        f"{county + ' County' if county else 'this sponsor’s service area'}. It is not an "
        "endorsement, requirement, or recommendation by TxtAnOffer, the buyer, or the seller "
        "— you are free to choose any title company or lender for this transaction."
    )
    disc_lines = _wrap_text(c, disc_text, "Helvetica", 7.5, content_w * 0.85)
    c.setFillColor(pal["text_dim"])
    c.setFont("Helvetica", 7.5)
    for line in disc_lines:
        c.drawCentredString(cx, disc_y, line)
        disc_y -= 0.14 * inch

    footer_y = 0.55 * inch
    c.setStrokeColor(pal["divider"])
    c.setLineWidth(0.5)
    c.line(margin, footer_y + 0.15*inch, width - margin, footer_y + 0.15*inch)
    c.setFillColor(pal["text_dim"])
    c.setFont("Helvetica", 7)
    c.drawCentredString(cx, footer_y, "Sponsored placement · via TxtAnOffer · txtanoffer.com")

    c.save()
    buffer.seek(0)
    return buffer.getvalue()
