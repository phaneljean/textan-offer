"""
cover_page.py — Generate premium cover page for TREC contracts
"""
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor
from datetime import datetime
import io

# Brand colors (brass/ink from your design system)
BRASS = HexColor("#A9772F")
INK = HexColor("#171B24")
PAPER = HexColor("#F3EEDF")
TEXT_MUTED = HexColor("#847C68")

def generate_cover_page(parsed: dict, agent: dict) -> bytes:
    """
    Generate a professional cover page PDF.
    Returns PDF bytes that can be merged with the TREC form.
    """
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter

    # Background
    c.setFillColor(PAPER)
    c.rect(0, 0, width, height, fill=1, stroke=0)

    # Top accent line
    c.setFillColor(BRASS)
    c.rect(0, height - 3, width, 3, fill=1, stroke=0)

    # Logo/Brand
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 24)
    c.drawCentredString(width/2, height - 1.5*inch, "TEXTANOFFER")

    c.setFillColor(TEXT_MUTED)
    c.setFont("Helvetica", 10)
    c.drawCentredString(width/2, height - 1.8*inch, "Professional Offer Package")

    # Brass divider
    c.setStrokeColor(BRASS)
    c.setLineWidth(1)
    c.line(2*inch, height - 2.2*inch, width - 2*inch, height - 2.2*inch)

    # Property Info
    y = height - 3*inch
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 18)
    c.drawCentredString(width/2, y, parsed.get('address', 'Property Address'))

    # Deal Summary
    y -= 0.6*inch
    c.setFont("Helvetica", 14)
    price_text = f"${parsed.get('price', 0):,}"
    close_text = f"{parsed.get('close_days', 0)}-day close"
    c.drawCentredString(width/2, y, f"{price_text}  •  {close_text}")

    # Property Details Box
    y -= 1*inch
    box_left = 2*inch
    box_width = width - 4*inch
    box_height = 1.2*inch

    c.setFillColor(HexColor("#FFFDF7"))
    c.setStrokeColor(BRASS)
    c.setLineWidth(1)
    c.roundRect(box_left, y - box_height, box_width, box_height, 4, fill=1, stroke=1)

    # Property details inside box
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 10)
    y_inside = y - 0.3*inch
    c.drawString(box_left + 0.3*inch, y_inside, "PROPERTY DETAILS")

    c.setFont("Helvetica", 10)
    y_inside -= 0.25*inch
    details = [
        f"Beds/Baths: {parsed.get('bed', 'N/A')} / {parsed.get('bath', 'N/A')}",
        f"Square Feet: {parsed.get('sqft', 'N/A'):,}" if parsed.get('sqft') else "Square Feet: N/A",
        f"City: {parsed.get('city', 'N/A')}",
        f"County: {parsed.get('county', 'N/A')}"
    ]
    for detail in details:
        c.drawString(box_left + 0.3*inch, y_inside, detail)
        y_inside -= 0.2*inch

    # Financial Summary Box
    y -= 2.5*inch
    c.setFillColor(HexColor("#FFFDF7"))
    c.roundRect(box_left, y - box_height, box_width, box_height, 4, fill=1, stroke=1)

    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 10)
    y_inside = y - 0.3*inch
    c.drawString(box_left + 0.3*inch, y_inside, "FINANCIAL SUMMARY")

    c.setFont("Helvetica", 10)
    y_inside -= 0.25*inch
    financials = [
        f"Down Payment: ${parsed.get('down_payment_amount', 0):,} ({parsed.get('down_payment_pct', 0)*100:.0f}%)",
        f"Loan Amount: ${parsed.get('loan_amount', 0):,}",
        f"Earnest Money: ${parsed.get('earnest_money', 0):,}",
        f"Option Fee: ${parsed.get('option_fee', 0)}"
    ]
    for item in financials:
        c.drawString(box_left + 0.3*inch, y_inside, item)
        y_inside -= 0.2*inch

    # Agent Info
    y -= 2*inch
    c.setFillColor(TEXT_MUTED)
    c.setFont("Helvetica", 9)
    if agent.get('name'):
        c.drawCentredString(width/2, y, f"Prepared by: {agent['name']}")
        y -= 0.2*inch
    if agent.get('brokerage'):
        c.drawCentredString(width/2, y, agent['brokerage'])
        y -= 0.2*inch
    if agent.get('phone'):
        c.drawCentredString(width/2, y, agent['phone'])

    # Footer
    c.setFont("Helvetica", 8)
    c.setFillColor(TEXT_MUTED)
    timestamp = datetime.now().strftime("%B %d, %Y at %I:%M %p")
    c.drawCentredString(width/2, 1*inch, f"Generated: {timestamp}")
    c.drawCentredString(width/2, 0.7*inch, "This package includes the completed TREC No. 20-19 contract")
    c.drawCentredString(width/2, 0.5*inch, "Draft only — agent must review before signing")

    c.save()
    buffer.seek(0)
    return buffer.getvalue()
