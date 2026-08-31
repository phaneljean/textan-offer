"""
tc_audit.py — Standalone field-completeness audit for a TREC 20-19 AcroForm
PDF uploaded by a transaction coordinator, independent of this app's own
offer-generation flow (see app.py's process_offer() / pdf_filler.py).

This is NOT the same check as pdf_validator.validate_offer_pdf(): that
function checks a PDF THIS APP just generated against the `parsed` dict THIS
APP's own parser produced -- it has an external source of truth. A TC's
uploaded file could have been filled by any tool, so there is no ground
truth to check it against here. v1 is therefore internal-consistency-only:
did the fields this app has already rect-verified (see pdf_filler.py's
FIELD_MAP comments) actually get filled in.

Only fields with a confirmed on-page position are checked. Effective Date,
per-page initials, cross-document (addendum) checks, and earnest-money
receipts are all explicitly OUT of scope for v1 -- their field mapping has
never been rect-verified, and a wrong "complete" signal on an unverified
field is worse than no signal at all (see CLAUDE.md / QA_SPEC.md: TREC's
auto-generated field /T names routinely lie about their on-page position).

v1 also assumes the uploaded PDF is AcroForm-fillable (not a flattened scan)
and was filled using field names matching this app's own 20-19_2.pdf
template. A PDF from a different tool/source/form revision may use entirely
different internal field names -- if too few of the checked fields are even
present in the uploaded file, this reports the file as unrecognized rather
than claiming everything on it is "missing".
"""
from pypdf import PdfReader
from pdf_validator import _read_values, _is_checked, _money_to_int
from pdf_filler import FIELD_MAP
from financing_addendum import FIELD_MAP as FA_FIELDS

# (FIELD_MAP key, message shown to the TC, blocking)
# "blocking" mirrors the same product decision pdf_validator.py already
# made for these exact fields: buyer/seller legal names are never collected
# by this app's own generation flow either, so their absence is a warning,
# not a blocker, here too.
CHECKED_FIELDS = [
    ("address", "Section 2A: Property address is blank", True),
    ("city", "Section 2A: City is blank", True),
    ("county", "Section 2A: County is blank -- title will kick back the file without this", True),
    ("buyer_name", "Section 1: Buyer legal name is blank", False),
    ("seller_name", "Section 1: Seller legal name is blank", False),
    ("escrow_agent_name", "Section 5A: Escrow Agent name is blank", True),
    ("earnest_money_amount", "Section 5A: Earnest money amount is blank", True),
    ("option_fee_amount", "Section 5A: Option fee amount is blank", True),
    ("title_company", "Section 6A: Title Company is blank", True),
]

# Below this many matched field names, treat the upload as a template we
# don't recognize rather than reporting every checked field as "missing" --
# a PDF from a different tool/source won't use this app's field names at all.
MIN_MATCHED_FIELDS = 3

# If most of the core required fields (address, county, title company,
# escrow agent, earnest money, option fee -- NOT initials/Effective
# Date/addendum, which a nearly-finished file can legitimately still be
# missing right before closing) are blank, this isn't a file with a few
# fixable gaps -- it's an essentially blank draft. Cheaper for the TC to
# regenerate cleanly than to chase down that many individual corrections.
CORE_BLOCKING_FIELDS = sum(1 for _, _, blocking in CHECKED_FIELDS if blocking)
BLANK_DRAFT_THRESHOLD = 0.7  # fraction of CORE_BLOCKING_FIELDS missing

# Effective Date -- TREC 20-19 page 10 of 12: "EXECUTED the ___ day of ___,
# 20__ (Effective Date). (BROKER: FILL IN THE DATE OF FINAL ACCEPTANCE.)"
# Rect-verified 2026-08-30 by rendering distinct markers into each field and
# confirming visually against the printed blank -- none of these 3 raw names
# describe their own role (another instance of TREC's export-tool naming
# lying about position). NOT in pdf_filler.py's FIELD_MAP -- this app never
# fills Effective Date (it's the broker's to fill in on final acceptance,
# same reasoning as buyer/seller signatures), so it was never rect-verified
# until now. Note: FIELD_MAP's "closing_year_suffix": "20_2" entry is a
# stale/unused mismap -- "20_2" is actually this Effective Date year field,
# not a closing-date field (fill_offer_pdf() never references that key at
# all; closing date is drawn entirely via reportlab overlay).
EFFECTIVE_DATE_FIELDS = {
    "day": "EXECUTED the",
    "month": "day of",
    "year": "20_2",
}

# Initials-for-identification quads -- 4 raw fields per page (Buyer1, Buyer2,
# Seller1, Seller2), rect-verified 2026-08-30 by rendering distinct COL1-4
# markers into each candidate field and visually confirming against the
# printed "Initialed for identification by Buyer ___ ___ and Seller ___ ___"
# line. IMPORTANT: field names do NOT reliably match role -- on pages 8 and 9
# (indices 7, 8) the field literally named "and Seller_*" renders in the
# BUYER2 position, not Seller1 (confirmed by render, not by name). Trust this
# table's column position, never the field name text, same rule as
# everywhere else in this codebase's FIELD_MAP.
# Printed page numbers confirmed via each page's own footer text.
INITIALS_PAGES = [
    ("Page 1 of 12", "Initialed for identification by Buyer", "undefined_8", "and Seller", "undefined_9"),
    ("Page 4 of 12", "Initialed for identification by Buyer_2", "undefined_14", "and Seller_4", "undefined_15"),
    ("Page 5 of 12", "Initialed for identification by Buyer_3", "Buyers Expenses as allowed by the lender", "and Seller_5", "undefined_16"),
    ("Page 6 of 12", "Initialed for identification by Buyer_4", "undefined_17", "and Seller_6", "undefined_18"),
    ("Page 8 of 12", "Initialed for identification by Buyer_521", "and Seller_18", "undefined_2219", "undefined_2322"),
    ("Page 9 of 12", "Initialed for identification by Buyer_5", "and Seller_7", "undefined_22", "undefined_23"),
]

# Same quad on the 40-11 Third Party Financing Addendum's own page 1 of 2 --
# rect-verified 2026-08-30 the same way. Only checked when the addendum is
# actually attached (its fields are namespaced "FA_" + raw name by
# financing_addendum.py at merge time -- see pdf_validator.py's same FA_
# convention). No buyer/seller NAME field exists anywhere on this addendum
# template (checked directly -- only loan-amount and initials fields do), so
# a name cross-check between the main contract and this addendum is not
# buildable; only the addendum's own initials-completeness is checked here.
FA_PREFIX = "FA_"
FA_INITIALS_PAGE = ("40-11 addendum", "Initialed for identification by Buyer", "undefined_2", "and Seller", "undefined_3")

# Addendum-vs-contract internal consistency. Both sides of each comparison
# live in the SAME uploaded document, so this needs no external "parsed"
# ground truth (unlike pdf_validator.py's equivalent check, which compares
# against parsed["loan_amount"] because it's validating a freshly-generated
# draft rather than an arbitrary upload). Loan amount uses FA_FIELDS'
# "first_loan_amount" (already used -- and thus already trusted -- by
# financing_addendum.py's own fill logic) against pdf_filler.py's
# already-verified "loan_amount" (Section 3B). Checkbox names likewise
# reuse pdf_filler.py's already rect-verified "third_party_financing_3b"
# (Sec 3B row) and "third_party_financing" (Sec 22 addenda list).


def _check_initials_quad(values: dict, page_label: str, b1: str, b2: str, s1: str, s2: str) -> list:
    issues = []
    if not values.get(b1, "").strip() or not values.get(b2, "").strip():
        issues.append({"severity": "blocker", "message": f"{page_label}: Buyer initials missing"})
    if not values.get(s1, "").strip() or not values.get(s2, "").strip():
        issues.append({"severity": "blocker", "message": f"{page_label}: Seller initials missing"})
    return issues


def check_tc_file(pdf_path: str) -> dict:
    """Audits an uploaded TREC 20-19 AcroForm PDF for missing required
    fields. Returns {"recognized": bool, "complete": bool, "issues": [...]}.
    Raises whatever pypdf raises on a file that isn't a readable PDF at all --
    callers should catch that and turn it into a 400, not a 500."""
    # Page count only, for the upload-result metadata bar -- a second,
    # cheap PdfReader open (separate from _read_values' own) rather than
    # threading a reader object through pdf_validator.py's private helper.
    page_count = len(PdfReader(pdf_path).pages)

    values = _read_values(pdf_path)

    matched = sum(1 for key, _, _ in CHECKED_FIELDS if FIELD_MAP[key] in values)
    if matched < MIN_MATCHED_FIELDS:
        return {
            "recognized": False,
            "complete": False,
            "issues": [{
                "severity": "blocker",
                "message": (
                    "This doesn't look like a TREC 20-19 form we recognize -- "
                    "field names didn't match our template. Only AcroForm-fillable "
                    "20-19 PDFs are supported in this version (not scanned or flattened files)."
                ),
            }],
            "looks_like_blank_draft": False,
            "page_count": page_count,
            "has_addendum": False,
        }

    issues = []
    core_missing = 0
    for key, message, blocking in CHECKED_FIELDS:
        val = values.get(FIELD_MAP[key], "").strip()
        if not val:
            issues.append({"severity": "blocker" if blocking else "warning", "message": message})
            if blocking:
                core_missing += 1

    # Effective Date
    missing_parts = [label for label, raw in EFFECTIVE_DATE_FIELDS.items() if not values.get(raw, "").strip()]
    if missing_parts:
        issues.append({"severity": "blocker", "message": "Page 10 of 12: Effective Date is blank"})

    # Initials for identification, main contract
    for page_label, b1, b2, s1, s2 in INITIALS_PAGES:
        issues.extend(_check_initials_quad(values, page_label, b1, b2, s1, s2))

    # Addendum-attached fields are FA_-prefixed; a cash offer has none of them.
    has_addendum = any(k.startswith(FA_PREFIX) for k in values)

    # Initials on the 40-11 addendum -- only if actually attached.
    if has_addendum:
        label, b1, b2, s1, s2 = FA_INITIALS_PAGE
        fa_values = {k: values.get(FA_PREFIX + k, "") for k in (b1, b2, s1, s2)}
        issues.extend(_check_initials_quad(fa_values, label, b1, b2, s1, s2))

    # 1. Loan amount: main contract Section 3B vs. 40-11 principal amount.
    if has_addendum:
        main_loan = values.get(FIELD_MAP["loan_amount"], "").strip()
        fa_loan = values.get(FA_PREFIX + FA_FIELDS["first_loan_amount"], "").strip()
        if main_loan and fa_loan and _money_to_int(main_loan) != _money_to_int(fa_loan):
            issues.append({
                "severity": "blocker",
                "message": f"Section 3B financing amount ({main_loan}) doesn't match the 40-11 addendum's principal amount ({fa_loan})",
            })

    # 2. Attachment consistency: the "Third Party Financing Addendum"
    # checkboxes on the main contract (Sec 3B row + Sec 22 addenda list)
    # should be checked if and only if a 40-11 is actually attached.
    checked_3b = _is_checked(values, FIELD_MAP["third_party_financing_3b"])
    checked_22 = _is_checked(values, FIELD_MAP["third_party_financing"])
    if has_addendum:
        if not checked_3b:
            issues.append({"severity": "blocker", "message": "Section 3B: Third Party Financing Addendum checkbox not checked, but a 40-11 addendum is attached"})
        if not checked_22:
            issues.append({"severity": "blocker", "message": "Section 22: Third Party Financing Addendum checkbox not checked, but a 40-11 addendum is attached"})
    else:
        if checked_3b:
            issues.append({"severity": "blocker", "message": "Section 3B: Third Party Financing Addendum checkbox is checked, but no 40-11 addendum is attached"})
        if checked_22:
            issues.append({"severity": "blocker", "message": "Section 22: Third Party Financing Addendum checkbox is checked, but no 40-11 addendum is attached"})

    blocking_issues = [i for i in issues if i["severity"] == "blocker"]
    return {
        "recognized": True,
        "complete": len(blocking_issues) == 0,
        "issues": issues,
        "looks_like_blank_draft": core_missing / CORE_BLOCKING_FIELDS >= BLANK_DRAFT_THRESHOLD,
        "page_count": page_count,
        "has_addendum": has_addendum,
    }
