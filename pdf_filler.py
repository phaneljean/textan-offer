"""
pdf_filler.py — fills TREC No. 20-19 (One to Four Family Residential
Contract, Resale) using its real AcroForm fields.
Correction from earlier attempts: the draft copy (20-19_draft_0.pdf,
with visible redlines) reported ZERO fillable fields, so we built a
coordinate-overlay approach. The clean, final published PDF
(20-19_2.pdf) actually HAS 281 real AcroForm fields. Field-based
filling is far more robust than coordinate overlay -- it doesn't break
if TREC nudges spacing in a future revision -- so we use that instead.
Field names below were confirmed by inspecting 20-19_2.pdf directly
(inspect_fields() at the bottom of this file reproduces that check).
TREC's PDF export tool auto-names fields from nearby text, which is
why some are verbose ("Texas known as") and some are generic
("undefined_3") -- that's TREC's export, not a choice we made.
"""
import os
from datetime import datetime, timedelta
from pypdf import PdfReader, PdfWriter

TEMPLATE_PATH = os.environ.get("TREC_TEMPLATE_PATH", "20-19_2.pdf")
OUTPUT_DIR = os.environ.get("OFFER_OUTPUT_DIR", "generated_offers")

# Confirmed against 20-19_2.pdf (the clean, final, mandatory-as-of-7/1/2026 form).
FIELD_MAP = {
    "address": "Texas known as", # Paragraph 2A "known as ___" line
    "sales_price": "undefined_3", # Paragraph 3A cash portion $ blank
    "closing_date": "A The closing of the sale will be on or before", # Paragraph 9A
}

def fill_offer_pdf(parsed: dict, agent_phone: str) -> str:
    if not os.path.exists(TEMPLATE_PATH):
        raise FileNotFoundError(
            f"Template not found at {TEMPLATE_PATH}. "
            "Set TREC_TEMPLATE_PATH to the clean, final 20-19 PDF."
        )
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    reader = PdfReader(TEMPLATE_PATH)
    writer = PdfWriter()
    writer.append(reader)
    
    values = {}
    if parsed.get("address"):
        values[FIELD_MAP["address"]] = parsed["address"]
    if parsed.get("price") is not None:
        values[FIELD_MAP["sales_price"]] = f"{parsed['price']:,}"
    if parsed.get("close_days") is not None:
        close_dt = datetime.now() + timedelta(days=parsed["close_days"])
        values[FIELD_MAP["closing_date"]] = close_dt.strftime("%B %d, %Y")
    
    # Buyer/seller legal names, earnest money, option fee, and financing
    # terms aren't in the SMS format yet -- intentionally left blank for
    # the agent to complete before this goes anywhere near a signature.
    
    for page in writer.pages:
        writer.update_page_form_field_values(page, values)
    
    # Ensure filled values render even if the PDF viewer doesn't regenerate appearances.
    if writer._root_object.get("/AcroForm") is not None:
        writer._root_object["/AcroForm"][__import__("pypdf").generic.NameObject("/NeedAppearances")] = \
            __import__("pypdf").generic.BooleanObject(True)
    
    safe_addr = "".join(ch for ch in (parsed.get("address") or "offer") if ch.isalnum())[:30]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(OUTPUT_DIR, f"TREC20-19_{safe_addr}_{timestamp}.pdf")
    
    with open(out_path, "wb") as f:
        writer.write(f)
    
    return out_path

def inspect_fields(pdf_path: str = TEMPLATE_PATH):
    """Reproduces the field discovery used to build FIELD_MAP above."""
    reader = PdfReader(pdf_path)
    fields = reader.get_fields()
    if not fields:
        print("No fillable fields found.")
        return
    print(f"{len(fields)} fields found:")
    for name, f in fields.items():
        print(f" {name!r:60} type={f.get('/FT')}")

if __name__ == "__main__":
    sample = {
        "price": 725000,
        "down_payment_pct": 0.03,
        "close_days": 21,
        "address": "1740 Grand Ave, Austin, TX",
    }
    path = fill_offer_pdf(sample, "+15125550100")
    print("Wrote:", path)

