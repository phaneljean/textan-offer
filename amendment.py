"""
amendment.py — Fill TREC No. 39-11 Amendment to Contract.

Used to change price or closing date on a contract TextAnOffer already
generated. Only the single field the agent asked to change gets filled;
everything else on the form (repairs, financing-amount changes, option
fee, buyer-approval deadline, other modifications) stays blank for the
agent to complete -- same policy as financing_addendum.py: don't put
numbers on a legal document that nobody actually agreed to.

Field names/positions confirmed by rendering 39-11.pdf and cross-checking
against each field's /Rect position on the page (see FIELD_MAP comments).
"""
import os
import io
import re
from datetime import datetime, timedelta
from deadlines import offer_closing_date, add_trec_days
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, BooleanObject

TEMPLATE_PATH = os.environ.get("AMENDMENT_TEMPLATE_PATH", "39-11.pdf")
OUTPUT_DIR = os.environ.get("OFFER_OUTPUT_DIR", "generated_offers")

FIELD_MAP = {
    "address": "Street Address and City",

    # Item (1): Sales Price change -- A (cash)/B (financing) left blank,
    # only C (total Sales Price) is filled.
    "price_checkbox": "1 The Sales Price in Paragraph 3 of the contract is",
    "price_total": "undefined_3",

    # Item (3): Closing date change (Paragraph 9)
    "close_checkbox": "3 The date in Paragraph 9 of the contract is changed to",
    "close_date": "date 5",
    "close_year_suffix": "20_25",
}


CO_AMEND_MESSAGE = ("Amendments for Colorado offers aren't supported yet -- use the Colorado Real Estate "
                    "Commission's Agreement to Amend/Extend Contract.")


def fill_amendment_pdf(original_offer: dict, amendment: dict) -> str:
    """
    original_offer: a row from offers_db.get_offers_for_phone() -- has
        address, price, close_days, created_at, filename.
    amendment: {"field": "price"|"close", "value": int}
        price -> new total sales price in dollars
        close -> number of days to extend the ORIGINAL closing date by
    Returns the path of the generated PDF.
    """
    if (original_offer.get("filename") or "").startswith("CBS1_"):
        # A TREC 39-11 must never amend a Colorado contract.
        raise ValueError(CO_AMEND_MESSAGE)
    if not os.path.exists(TEMPLATE_PATH):
        raise FileNotFoundError(f"TREC 39-11 template not found at {TEMPLATE_PATH}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    reader = PdfReader(TEMPLATE_PATH)
    writer = PdfWriter()
    writer.append(reader)

    values = {FIELD_MAP["address"]: original_offer.get("address", "")}
    checkboxes = []

    if amendment["field"] == "price":
        checkboxes.append(FIELD_MAP["price_checkbox"])
        # No leading "$" -- the printed 39-11 form already has "$" before
        # this blank (same doubled-dollar-sign bug fixed 2026-09-09 in
        # pdf_filler.py and financing_addendum.py).
        values[FIELD_MAP["price_total"]] = f"{amendment['value']:,}"
        suffix = f"price{amendment['value']}"
    else:
        # The original is whatever the 20-19 actually printed
        # (deadlines.offer_closing_date); the extended date gets the same
        # move-off-the-weekend rule.
        original_close = offer_closing_date(original_offer.get("close_days", 0), original_offer["created_at"])
        new_close = add_trec_days(original_close, amendment["value"])
        checkboxes.append(FIELD_MAP["close_checkbox"])
        # Form pre-prints "20", so only the 2-digit year suffix is filled here.
        values[FIELD_MAP["close_date"]] = new_close.strftime("%B %d,")
        values[FIELD_MAP["close_year_suffix"]] = new_close.strftime("%y")
        suffix = f"close{new_close.strftime('%Y%m%d')}"

    for page in writer.pages:
        writer.update_page_form_field_values(page, values)

    for page in writer.pages:
        if "/Annots" not in page:
            continue
        for annot_ref in page["/Annots"]:
            annot = annot_ref.get_object()
            name = str(annot.get("/T", ""))
            if name in checkboxes:
                annot[NameObject("/V")] = NameObject("/On")
                annot[NameObject("/AS")] = NameObject("/On")

    acroform = writer._root_object.get("/AcroForm")
    if hasattr(acroform, 'get_object'):
        acroform = acroform.get_object()
    if acroform is not None:
        acroform[NameObject("/NeedAppearances")] = BooleanObject(True)

    safe_addr = "".join(ch for ch in (original_offer.get("address") or "amendment") if ch.isalnum())[:30]
    # Same collision fix as offers: unique, never-overwriting filename.
    from pdf_filler import open_unique_output
    f, path = open_unique_output(OUTPUT_DIR, f"AMEND39-11_{safe_addr}_{suffix}")
    with f:
        writer.write(f)

    return path
