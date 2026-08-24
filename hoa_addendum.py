"""
hoa_addendum.py — Attach TREC 36-10, Addendum for Property Subject to
Mandatory Membership in a Property Owners Association, when the agent's
text signals the property has an HOA. Unlike the Water Disclosure (always
attached), this one is conditional -- not every property has a mandatory
HOA, so attaching it to every offer would be inaccurate on a legal document.

36-10.pdf has zero AcroForm fields (a flat form), so this uses the same
reportlab-overlay technique as water_disclosure.py, not field-filling.

Only the property address is filled in -- every substantive section here
(how Subdivision Information gets delivered, the Association transfer-fee
cap, who pays the title company, the HOA's own name/phone) requires facts
this app has no way to collect from a one-line SMS offer string. Guessing
at any of those would be worse than leaving them blank for the agent to
complete by hand -- same principle as the Water Disclosure and buyer/seller
names elsewhere in this app.
"""
import os
import io
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from water_disclosure import _full_address

TEMPLATE_PATH = os.environ.get("HOA_ADDENDUM_TEMPLATE_PATH", "36-10.pdf")


def fill_hoa_addendum(parsed: dict) -> bytes:
    if not os.path.exists(TEMPLATE_PATH):
        raise FileNotFoundError(f"TREC 36-10 template not found at {TEMPLATE_PATH}")

    reader = PdfReader(TEMPLATE_PATH)
    full_addr = _full_address(parsed)
    width, height = letter

    # "ADDENDUM TO CONTRACT CONCERNING THE PROPERTY AT ___" -- blank runs
    # centered, baseline just above the "(Street Address and City)" caption
    # (caption top ~134.3 on a 792pt-tall page, verified via pdfplumber).
    overlay_buf = io.BytesIO()
    c = canvas.Canvas(overlay_buf, pagesize=letter)
    c.setFont("Helvetica", 10)
    c.drawCentredString(width / 2, height - 132, full_addr)
    c.save()
    overlay_buf.seek(0)

    reader.pages[0].merge_page(PdfReader(overlay_buf).pages[0])

    buf = io.BytesIO()
    writer = PdfWriter()
    writer.append(reader)
    writer.write(buf)
    buf.seek(0)
    return buf.getvalue()
