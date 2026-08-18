"""
pdf_validator.py — Verifies a generated offer PDF actually has every
required field filled in, by reading the AcroForm values back out of the
FINISHED file rather than trusting that the fill step worked.

This exists because every real bug found in this app's PDF generation
(Section 21, Paragraph 5A, cross-document field-name collisions) looked
fine in the code and only showed up on rendering the actual output. This
module is that rendering check, automated and run on every send instead
of relying on a human to notice.

Per product decision (2026-08-17): Buyer/Seller legal names and the
Escrow Agent's mailing address are NEVER collected anywhere in this
app's data model (same as buyer/seller signatures -- the agent fills
them in by hand before presenting). Blocking send on those would make
"Email to Listing Agent" permanently unusable, so they're WARNINGS, not
blocking failures. Every other field in the QA spec is a hard block.
"""
import re
from datetime import datetime, timedelta

from pypdf import PdfReader

from pdf_filler import FIELD_MAP as TREC_FIELDS
from financing_addendum import FIELD_MAP as FA_FIELDS

FA_PREFIX = "FA_"  # matches the namespacing applied in financing_addendum.py


def _fa(key: str) -> str:
    return FA_PREFIX + FA_FIELDS[key]


def _read_values(pdf_path: str) -> dict:
    reader = PdfReader(pdf_path)
    fields = reader.get_fields() or {}
    return {name: str(f.get("/V") or "") for name, f in fields.items()}


def _read_text(pdf_path: str) -> str:
    reader = PdfReader(pdf_path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _is_checked(values: dict, name: str) -> bool:
    v = values.get(name, "")
    return v not in ("", "/Off", "Off")


def _money_to_int(text: str):
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def _checkbox_appearance_mismatches(pdf_path: str) -> list:
    """Finds checkboxes where /AS (the state we set to "check" the box) is
    NOT actually a key in the widget's own /AP/N appearance dictionary.

    Found 2026-08-18: this app's checkbox-filling code used to hardcode
    /AS to "/On" for every checkbox, but the 40-11's Buyer Approval
    checkbox's real on-state is "/Yes" -- so /AS="/On" doesn't match
    anything in /AP/N, and no appearance gets painted. /V still reads
    "/On" (so a naive presence check like the rest of this module thinks
    it's fine), but the box renders as unchecked in any spec-strict
    viewer (confirmed via Safari; PyMuPDF's renderer is lenient enough to
    paper over this, which is why it wasn't caught by this app's own
    rendered-output testing). pdf_filler.py and financing_addendum.py are
    now fixed to read each checkbox's real on-state instead of assuming
    "/On" -- this is the permanent regression guard for that bug class,
    covering any future checkbox this app didn't get to manually audit."""
    reader = PdfReader(pdf_path)
    mismatches = []
    for page in reader.pages:
        if "/Annots" not in page:
            continue
        for annot_ref in page["/Annots"]:
            annot = annot_ref.get_object()
            if annot.get("/FT") != "/Btn":
                continue
            state = annot.get("/AS")
            if state in (None, "/Off"):
                continue  # unchecked -- nothing to verify
            ap = annot.get("/AP")
            n_obj = ap["/N"].get_object() if ap and "/N" in ap else None
            valid_states = list(n_obj.keys()) if hasattr(n_obj, "keys") else []
            if state not in valid_states:
                name = annot.get("/T") or (
                    annot["/Parent"].get_object().get("/T") if "/Parent" in annot else "?"
                )
                mismatches.append(f'{name!r} set to {state!r}, but /AP only has {valid_states!r}')
    return mismatches


def validate_offer_pdf(pdf_path: str, parsed: dict) -> dict:
    """Returns {"ok": bool, "blocking": [str], "warnings": [str]}.
    "ok" reflects `blocking` only -- warnings never block sending."""
    values = _read_values(pdf_path)
    text = _read_text(pdf_path)
    blocking = []
    warnings = []

    for mismatch in _checkbox_appearance_mismatches(pdf_path):
        blocking.append(f"Checkbox appearance mismatch (will render unchecked in strict viewers like Safari): {mismatch}")

    def require(key, label):
        val = values.get(TREC_FIELDS[key], "").strip()
        if not val:
            blocking.append(label)
        return val

    # Section 1: Buyer/Seller legal names -- never auto-filled by design
    # (see module docstring), so this is a warning, not a blocking check.
    if not values.get(TREC_FIELDS["seller_name"], "").strip() or not values.get(TREC_FIELDS["buyer_name"], "").strip():
        warnings.append(
            "Section 1: Buyer and/or Seller legal name is blank -- fill in by hand before sending."
        )

    # Section 2A: Property
    require("address", "Section 2A: Property address")
    require("city", "Section 2A: City")
    require("county", "Section 2A: County")

    # Section 3: Sales Price A/B/C
    down_val = require("down_payment", "Section 3A: Cash portion of Sales Price")
    loan_val = require("loan_amount", "Section 3B: Sum of financing")
    price_val = require("sales_price", "Section 3C: Sales Price total")

    # Section 5: Earnest Money & Escrow Agent name (address is a warning --
    # never collected, see module docstring)
    require("escrow_agent_name", "Section 5A: Escrow Agent name")
    earnest_val = require("earnest_money_amount", "Section 5A: Earnest money amount")
    option_val = require("option_fee_amount", "Section 5A: Option fee amount")
    warnings.append(
        "Section 5A: Escrow Agent mailing address is never auto-filled -- add it by hand before sending."
    )

    # Section 6A: Title Company
    require("title_company", "Section 6A: Title Company")

    # Section 9: Closing Date -- drawn via reportlab overlay, not an AcroForm
    # field, so check by the same formatted text pdf_filler.py stamps.
    # Anchored to the offer's actual creation time when available (passed as
    # parsed["created_at"]), NOT datetime.now() -- the closing date is fixed
    # at generation time, so re-deriving it from "now" at validation time
    # drifts a day later for every day that passes before this check runs,
    # producing a false mismatch against a PDF that was generated correctly.
    if parsed.get("close_days") is not None:
        reference = parsed.get("created_at")
        if isinstance(reference, str):
            try:
                reference = datetime.fromisoformat(reference)
            except ValueError:
                reference = None
        if not isinstance(reference, datetime):
            reference = datetime.now()
        close_dt = reference + timedelta(days=parsed["close_days"])
        expected = close_dt.strftime("%B %d,")
        if expected not in text:
            blocking.append("Section 9: Closing Date")

    # Section 21: Buyer's agent contact (Seller side intentionally blank --
    # not known at draft stage, per spec)
    require("agent_address_p21", "Section 21: Buyer's agent address")
    require("agent_phone_p21", "Section 21: Buyer's agent phone")
    require("agent_email_p21", "Section 21: Buyer's agent email")

    # 40-11 Section 1: financing type checkbox + principal amount
    financing_type_labels = {
        "conventional": "Conventional", "texas_veterans": "Texas Veterans",
        "fha": "FHA", "usda": "USDA", "va": "VA Guaranteed", "reverse_mortgage": "Reverse Mortgage",
    }
    checked_type = next(
        (k for k in financing_type_labels if _is_checked(values, _fa(k))), None
    )
    if not checked_type:
        blocking.append("40-11 Section 1: Financing type checkbox")
    elif checked_type == "conventional":
        if not values.get(_fa("first_loan_amount"), "").strip():
            blocking.append("40-11 Section 1(A)(1): First mortgage principal amount")
    else:
        # FHA/VA/USDA/Texas Veterans/Reverse Mortgage principal amounts
        # aren't wired to a verified field yet (see financing_addendum.py) --
        # warn instead of blocking non-conventional offers entirely.
        warnings.append(
            f"40-11 Section 1: {financing_type_labels[checked_type]} selected -- "
            "principal amount isn't auto-filled for this financing type yet, add it by hand."
        )

    # 40-11 Section 2A: Buyer Approval -- exactly one of the pair checked
    subject = _is_checked(values, _fa("buyer_approval"))
    not_subject = _is_checked(values, _fa("buyer_approval_not_subject"))
    if subject == not_subject:  # both checked or neither checked
        blocking.append("40-11 Section 2A: Buyer Approval (exactly one box must be checked)")

    # --- Consistency checks ---

    if price_val and parsed.get("price") is not None:
        if _money_to_int(price_val) != int(parsed["price"]):
            blocking.append(
                f"Section 3C Sales Price ({price_val}) doesn't match the offer total (${parsed['price']:,})"
            )
    if loan_val and parsed.get("loan_amount") is not None:
        if _money_to_int(loan_val) != int(parsed["loan_amount"]):
            blocking.append(
                f"Section 3B financing ({loan_val}) doesn't match the offer loan amount (${parsed['loan_amount']:,})"
            )
        fa_loan = values.get(_fa("first_loan_amount"), "").strip()
        if fa_loan and _money_to_int(fa_loan) != int(parsed["loan_amount"]):
            blocking.append(
                f"40-11 principal amount ({fa_loan}) doesn't match Section 3B (${parsed['loan_amount']:,})"
            )
    if down_val and parsed.get("down_payment_amount") is not None:
        if _money_to_int(down_val) != int(parsed["down_payment_amount"]):
            blocking.append(
                f"Section 3A cash portion ({down_val}) doesn't match the offer down payment (${parsed['down_payment_amount']:,})"
            )
    if earnest_val and parsed.get("earnest_money") is not None:
        if _money_to_int(earnest_val) != int(parsed["earnest_money"]):
            blocking.append("Section 5A earnest money doesn't match the offer's earnest money amount")
    if option_val and parsed.get("option_fee") is not None:
        if _money_to_int(option_val) != int(parsed["option_fee"]):
            blocking.append("Section 5A option fee doesn't match the offer's option fee amount")

    # Financing checkbox consistency: 3B row + Section 22 addenda list must
    # both be checked whenever a financing addendum is actually attached.
    has_loan = bool(parsed.get("loan_amount") and parsed["loan_amount"] > 0)
    if has_loan:
        if not _is_checked(values, TREC_FIELDS["third_party_financing_3b"]):
            blocking.append("Section 3B: Third Party Financing Addendum checkbox not checked")
        if not _is_checked(values, TREC_FIELDS["third_party_financing"]):
            blocking.append("Section 22: Third Party Financing Addendum checkbox not checked")

    # No stray values on any "Initialed by Buyer/Seller" line, anywhere in
    # either merged document -- this is the generalized, permanent guard
    # for the exact bug class found 2026-08-17 (down payment leaking onto
    # the 40-11's Seller-initials line via a field-name collision).
    for name, val in values.items():
        if "initial" in name.lower() and val.strip():
            blocking.append(f'Stray value "{val.strip()}" found on an initials line (field: {name!r})')

    return {"ok": len(blocking) == 0, "blocking": blocking, "warnings": warnings}
