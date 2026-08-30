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
from pdf_validator import _read_values
from pdf_filler import FIELD_MAP

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


def check_tc_file(pdf_path: str) -> dict:
    """Audits an uploaded TREC 20-19 AcroForm PDF for missing required
    fields. Returns {"recognized": bool, "complete": bool, "issues": [...]}.
    Raises whatever pypdf raises on a file that isn't a readable PDF at all --
    callers should catch that and turn it into a 400, not a 500."""
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
        }

    issues = []
    for key, message, blocking in CHECKED_FIELDS:
        val = values.get(FIELD_MAP[key], "").strip()
        if not val:
            issues.append({"severity": "blocker" if blocking else "warning", "message": message})

    blocking_issues = [i for i in issues if i["severity"] == "blocker"]
    return {
        "recognized": True,
        "complete": len(blocking_issues) == 0,
        "issues": issues,
    }
