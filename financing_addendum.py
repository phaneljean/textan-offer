"""
financing_addendum.py — Fill TREC 40-11 Third Party Financing Addendum
Auto-generates alongside the main contract when loan_amount > 0.
"""
import os
import io
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, BooleanObject

TEMPLATE_PATH = os.environ.get("FINANCING_TEMPLATE_PATH", "40-11.pdf")

FIELD_MAP = {
    "address_p1": "Street Address and City",
    "address_p2": "Address of Property",

    # Section 1: Financing type checkboxes
    # NOTE: TREC's PDF export mis-named the VA/USDA/Reverse-Mortgage checkbox
    # widgets -- their /T field names don't match their on-page position
    # (confirmed by cross-checking each widget's /Rect against the actual
    # printed layout). Mapped here by verified position, not by trusting the
    # field name text.
    "conventional": "1 Conventional Financing",
    "first_mortgage": "a A first mortgage loan in the principal amount of",
    "second_mortgage": "b A second mortgage loan in the principal amount of",
    "texas_veterans": "2 Texas Veterans Loan A loans from the Texas Veterans Land Board of",
    "fha": "3 FHA Insured Financing A Section",
    "va": "6 Reverse Mortgage Financing A reverse mortgage loan also known as a Home Equity",
    "usda": "4 VA Guaranteed Financing A VA guaranteed loan of not less than",
    "reverse_mortgage": "5 USDA Guaranteed Financing A USDAguaranteed loan of not less than",

    # Section 1A(1): Conventional first mortgage fields
    "first_loan_amount": "years with interest not to exceed",
    "first_due_years": "any financed PMI premium due in full in 1",
    "first_rate": "any financed PMI premium due in full in 2",
    "first_rate_years": "per annum for the first",
    "first_origination": "shown on Buyers Loan Estimate for the loan not to exceed",

    # Section 2A: Buyer Approval
    "buyer_approval": "This contract is subject to Buyer obtaining Buyer Approval If Buyer cannot obtain Buyer",
    "buyer_approval_days": "Check Box2",
}


def fill_financing_addendum(parsed: dict) -> bytes:
    if not os.path.exists(TEMPLATE_PATH):
        raise FileNotFoundError(f"TREC 40-11 template not found at {TEMPLATE_PATH}")

    reader = PdfReader(TEMPLATE_PATH)
    writer = PdfWriter()
    writer.append(reader)

    import re
    values = {}
    checkboxes = []

    # Address on both pages
    addr = parsed.get("address", "")
    addr_clean = re.sub(r',?\s*\b(TX|Texas)\b', '', addr, flags=re.IGNORECASE).strip(' ,')
    city = parsed.get("city", "")
    # Avoid doubling city if it's already in the address string
    if city and city.lower() not in addr_clean.lower():
        full_addr = f"{addr_clean}, {city}, TX"
    elif city:
        full_addr = f"{addr_clean}, TX"
    else:
        full_addr = addr_clean

    values[FIELD_MAP["address_p1"]] = full_addr
    values[FIELD_MAP["address_p2"]] = full_addr

    # Financing type checkbox -- defaults to Conventional when the agent
    # didn't specify one (or said "cash", which this app's model doesn't
    # support: loan_amount is always > 0 since down_payment_pct tops out at
    # 50%, so there's always a loan to describe here regardless). Loan amount
    # is real (derived from the actual offer), but interest rate, term, and
    # origination cap are terms nobody has agreed to yet -- left blank for
    # the agent to fill in, same as buyer/seller names and earnest money
    # elsewhere in the app.
    loan_amount = parsed.get("loan_amount", 0)
    financing_type = parsed.get("financing_type") or "conventional"
    if loan_amount > 0:
        if financing_type == "fha":
            checkboxes.append(FIELD_MAP["fha"])
        elif financing_type == "va":
            checkboxes.append(FIELD_MAP["va"])
        else:
            checkboxes.append(FIELD_MAP["conventional"])
            checkboxes.append(FIELD_MAP["first_mortgage"])
            values[FIELD_MAP["first_loan_amount"]] = f"${loan_amount:,}"

    # Buyer Approval: default to subject to approval, 21 days
    checkboxes.append(FIELD_MAP["buyer_approval"])

    # Fill text fields
    for page in writer.pages:
        writer.update_page_form_field_values(page, values)

    # Check checkboxes
    for page in writer.pages:
        if "/Annots" not in page:
            continue
        for annot_ref in page["/Annots"]:
            annot = annot_ref.get_object()
            name = str(annot.get("/T", ""))
            if name in checkboxes:
                annot[NameObject("/V")] = NameObject("/On")
                annot[NameObject("/AS")] = NameObject("/On")

    # Force appearance regeneration
    acroform = writer._root_object.get("/AcroForm")
    if hasattr(acroform, 'get_object'):
        acroform = acroform.get_object()
    if acroform is not None:
        acroform[NameObject("/NeedAppearances")] = BooleanObject(True)

    buf = io.BytesIO()
    writer.write(buf)
    buf.seek(0)
    return buf.getvalue()
