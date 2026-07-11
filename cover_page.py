"""
cover_page.py — Generate premium cover page for TREC contracts
Styled after classic real estate stationery: marble background, centered card.
"""
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, Color
from datetime import datetime, timedelta
import io

# Palette
MARBLE_BG = HexColor("#F0EBE3")
MARBLE_VEIN = HexColor("#DDD5C8")
CARD_BG = HexColor("#FAF8F5")
CARD_BORDER = HexColor("#E5DFD6")
INK = HexColor("#1A1A1A")
TEXT_SECONDARY = HexColor("#5C5549")
TEXT_MUTED = HexColor("#8A8279")
DIVIDER = HexColor("#C9C0B3")


def _draw_marble_bg(c, width, height):
    """Simulate subtle marble texture with gradient fills."""
    c.setFillColor(MARBLE_BG)
    c.rect(0, 0, width, height, fill=1, stroke=0)

    c.saveState()
    c.setFillColor(Color(0.82, 0.78, 0.73, alpha=0.12))
    c.translate(width * 0.2, height * 0.6)
    c.rotate(25)
    c.rect(-2*inch, -0.5*inch, 6*inch, 0.8*inch, fill=1, stroke=0)
    c.restoreState()

    c.saveState()
    c.setFillColor(Color(0.85, 0.80, 0.75, alpha=0.08))
    c.translate(width * 0.7, height * 0.3)
    c.rotate(-15)
    c.rect(-3*inch, -0.3*inch, 5*inch, 0.5*inch, fill=1, stroke=0)
    c.restoreState()


def _draw_card(c, x, y, w, h):
    """Draw the centered cream card with border."""
    c.setFillColor(CARD_BG)
    c.setStrokeColor(CARD_BORDER)
    c.setLineWidth(0.75)
    c.rect(x, y, w, h, fill=1, stroke=1)


def generate_cover_page(parsed: dict, agent: dict) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter

    _draw_marble_bg(c, width, height)

    # Card dimensions — centered
    card_w = 4.2 * inch
    card_h = 7.5 * inch
    card_x = (width - card_w) / 2
    card_y = (height - card_h) / 2

    _draw_card(c, card_x, card_y, card_w, card_h)

    # Content inside card
    cx = width / 2
    top = card_y + card_h

    # Agent name (large, uppercase, centered)
    y = top - 0.9 * inch
    agent_name = agent.get('name', 'REAL ESTATE AGENT').upper()
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 18)
    c.drawCentredString(cx, y, agent_name)

    # Subtitle
    y -= 0.35 * inch
    c.setFillColor(TEXT_SECONDARY)
    c.setFont("Helvetica", 10)
    c.drawCentredString(cx, y, "REAL ESTATE AGENT")

    # Divider line
    y -= 0.4 * inch
    c.setStrokeColor(DIVIDER)
    c.setLineWidth(0.75)
    div_w = 1.2 * inch
    c.line(cx - div_w/2, y, cx + div_w/2, y)

    # Property address
    y -= 0.55 * inch
    c.setFillColor(INK)
    c.setFont("Helvetica", 11)
    address = parsed.get('address', '')
    c.drawCentredString(cx, y, address)

    # City/County
    y -= 0.3 * inch
    city = parsed.get('city', '')
    county = parsed.get('county', '')
    location_parts = []
    if city:
        location_parts.append(city)
    if county:
        location_parts.append(f"{county} County")
    location_parts.append("TX")
    c.drawCentredString(cx, y, ", ".join(location_parts))

    # Spacing before deal terms
    y -= 0.6 * inch
    c.setFillColor(TEXT_SECONDARY)
    c.setFont("Helvetica", 10)

    # Deal terms — centered list
    price = parsed.get('price', 0)
    close_days = parsed.get('close_days', 0)
    close_date = (datetime.now() + timedelta(days=close_days)).strftime("%B %d, %Y")
    down_pct = parsed.get('down_payment_pct', 0)
    down_amt = parsed.get('down_payment_amount', 0)
    loan_amt = parsed.get('loan_amount', 0)
    earnest = parsed.get('earnest_money', 0)
    option = parsed.get('option_fee', 0)

    lines = [
        f"${price:,}",
        f"{down_pct*100:.0f}% Down  •  ${down_amt:,}",
        f"Loan: ${loan_amt:,}",
        f"Close: {close_date}",
        "",
        f"Earnest Money: ${earnest:,}",
        f"Option Fee: ${option}",
    ]

    for line in lines:
        if line == "":
            y -= 0.2 * inch
            continue
        c.drawCentredString(cx, y, line)
        y -= 0.32 * inch

    # Second divider
    y -= 0.25 * inch
    c.setStrokeColor(DIVIDER)
    c.line(cx - div_w/2, y, cx + div_w/2, y)

    # Agent contact info
    y -= 0.5 * inch
    c.setFillColor(TEXT_MUTED)
    c.setFont("Helvetica", 9)

    if agent.get('phone'):
        c.drawCentredString(cx, y, agent['phone'])
        y -= 0.26 * inch
    if agent.get('email'):
        c.drawCentredString(cx, y, agent['email'])
        y -= 0.26 * inch
    if agent.get('brokerage'):
        c.drawCentredString(cx, y, agent['brokerage'])
        y -= 0.26 * inch

    # Footer — draft notice (anchored to bottom of card)
    c.setFont("Helvetica", 7.5)
    c.setFillColor(TEXT_MUTED)
    c.drawCentredString(cx, card_y + 0.5*inch, "TREC No. 20-19  •  Draft — agent must review before signing")

    timestamp = datetime.now().strftime("%m/%d/%Y %I:%M %p")
    c.drawCentredString(cx, card_y + 0.28*inch, f"Generated {timestamp}")

    c.save()
    buffer.seek(0)
    return buffer.getvalue()
