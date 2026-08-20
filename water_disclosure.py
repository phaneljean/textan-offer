"""
water_disclosure.py — Attach TREC 61-0, Seller's Disclosure About Groundwater
and Surface Water Rights (new mandatory form, effective 07-01-2026, referenced
by Paragraph 7(I) of the 07-2026 20-19 template this app already fills).

61-0.pdf has zero AcroForm fields (a flat form, like 20-19's draft copy was),
so this uses the same reportlab-overlay technique as the closing-date and
page-11 address-header overlays in pdf_filler.py, not field-filling.

Only the property address is filled in -- every substantive disclosure
question here (Groundwater District status, well counts, ownership, surface
water rights, etc.) requires the seller's own first-hand knowledge, which
this app has no way to collect from an SMS offer string. Filling those in
would mean guessing at legal disclosures instead of leaving them for the
agent/seller to complete by hand, which is worse than leaving them blank --
same principle as buyer/seller names and earnest money elsewhere in this app.
"""
import os
import io
import re
from pypdf import PdfReader
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

TEMPLATE_PATH = os.environ.get("WATER_DISCLOSURE_TEMPLATE_PATH", "61-0.pdf")


def _full_address(parsed: dict) -> str:
    addr = parsed.get("address", "")
    addr_clean = re.sub(r',?\s*\b(TX|Texas)\b', '', addr, flags=re.IGNORECASE).strip(' ,')
    city = parsed.get("city", "")
    if city and city.lower() not in addr_clean.lower():
        return f"{addr_clean}, {city}, TX"
    elif city:
        return f"{addr_clean}, TX"
    return addr_clean


def fill_water_disclosure(parsed: dict) -> bytes:
    if not os.path.exists(TEMPLATE_PATH):
        raise FileNotFoundError(f"TREC 61-0 template not found at {TEMPLATE_PATH}")

    reader = PdfReader(TEMPLATE_PATH)
    full_addr = _full_address(parsed)
    width, height = letter

    # Page 1: "CONCERNING THE PROPERTY AT: ______" -- blank runs right of the
    # label, baseline just above the "(Street Address and City)" caption.
    overlay1_buf = io.BytesIO()
    c = canvas.Canvas(overlay1_buf, pagesize=letter)
    c.setFont("Helvetica", 10)
    c.drawString(225, height - 146, full_addr)
    c.save()
    overlay1_buf.seek(0)

    # Page 2: running header blank above the "(Address of Property)" caption,
    # centered like the caption below it.
    overlay2_buf = io.BytesIO()
    c = canvas.Canvas(overlay2_buf, pagesize=letter)
    c.setFont("Helvetica", 9)
    c.drawCentredString(width / 2, height - 47, full_addr)
    c.save()
    overlay2_buf.seek(0)

    reader.pages[0].merge_page(PdfReader(overlay1_buf).pages[0])
    reader.pages[1].merge_page(PdfReader(overlay2_buf).pages[0])

    buf = io.BytesIO()
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.append(reader)
    writer.write(buf)
    buf.seek(0)
    return buf.getvalue()
