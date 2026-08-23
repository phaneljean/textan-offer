"""
iabs.py — Attach TREC IABS 1-2, Information About Brokerage Services
(effective 01-01-2026, per Senate Bill 1968), to every generated offer
package.

IABS 1-2 has zero AcroForm fields (a flat form, like 61-0 and 20-19's draft
copy), so this uses the same reportlab-overlay technique as
water_disclosure.py and the closing-date/page-11 overlays in pdf_filler.py.

Only two lines are auto-filled, both taken straight from the agent's saved
profile with no guessing:
  - "Name of Sales Agent/Associate" row (Name, License No., Email, Phone) --
    this is literally the agent using TxtAnOffer, so their own profile data
    is authoritative.
  - "Name of Sponsoring Broker" row -- only the Name cell, from
    profile['brokerage']. The broker's own license number, email, and phone
    are not the agent's own and aren't collected anywhere in this app, so
    guessing them would be worse than leaving them blank.

"Name of Designated Broker" and "Name of Licensed Supervisor" (both marked
"if applicable" on the form) and the Date / initials line at the bottom are
left blank -- those require facts this app has no way to know (whether a
supervisor applies) or must be filled by the recipient at the time they
actually receive and acknowledge the notice, same reasoning as buyer/seller
names and earnest money elsewhere in this app.
"""
import os
import io
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

TEMPLATE_PATH = os.environ.get("IABS_TEMPLATE_PATH", "iabs.pdf")

# Column x-positions, aligned to the label captions printed on the form.
COL_NAME = 32
COL_LICENSE = 221
COL_EMAIL = 371
COL_PHONE = 523

# Baseline y for each blank line, sitting just above its label row.
ROW_SPONSORING_BROKER_Y = 165
ROW_SALES_AGENT_Y = 61


def fill_iabs(agent: dict) -> bytes:
    if not os.path.exists(TEMPLATE_PATH):
        raise FileNotFoundError(f"TREC IABS 1-2 template not found at {TEMPLATE_PATH}")

    reader = PdfReader(TEMPLATE_PATH)
    height = letter[1]

    overlay_buf = io.BytesIO()
    c = canvas.Canvas(overlay_buf, pagesize=letter)

    brokerage = agent.get("brokerage", "")
    if brokerage:
        c.setFont("Helvetica", 10)
        c.drawString(COL_NAME, ROW_SPONSORING_BROKER_Y, brokerage)

    name = agent.get("name", "")
    license_no = agent.get("license", "")
    email = agent.get("email", "")
    phone = agent.get("phone", "")

    if name:
        c.setFont("Helvetica", 10)
        c.drawString(COL_NAME, ROW_SALES_AGENT_Y, name)
    c.setFont("Helvetica", 9)
    if license_no:
        c.drawString(COL_LICENSE, ROW_SALES_AGENT_Y, license_no)
    if email:
        c.drawString(COL_EMAIL, ROW_SALES_AGENT_Y, email)
    if phone:
        c.drawString(COL_PHONE, ROW_SALES_AGENT_Y, phone)

    c.save()
    overlay_buf.seek(0)

    reader.pages[0].merge_page(PdfReader(overlay_buf).pages[0])

    buf = io.BytesIO()
    writer = PdfWriter()
    writer.append(reader)
    writer.write(buf)
    buf.seek(0)
    return buf.getvalue()
