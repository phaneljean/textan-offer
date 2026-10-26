"""Filled TREC 25-17 test files, built from TREC's own fillable 25-17_4.pdf
(tests/fixtures/trec_25-17_blank.pdf) the way txo-trec-qa's spec section 6
describes, by role from trec_25_17.MAP -- never by guessing a /T name.

    python3 -m tests.trec_25_17_fixtures <out_dir>   # writes the clean pair to look at

build(**changes) returns a path. A change is role=value: a string for a
text field (" " = blank: PyMuPDF ignores an empty value), True/False for a
checkbox. "initials_p3_seller2" etc. set one initials box; "header_p4" one
page header.
"""
import os
import sys
import tempfile

import fitz

import trec_25_17
from financing_addendum import FIELD_MAP as FA_FIELDS
import tc_audit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLANK = os.path.join(ROOT, "tests", "fixtures", "trec_25-17_blank.pdf")
FA_SOURCE = os.path.join(ROOT, "tests", "fixtures", "trec_40-11_clean.pdf")
ADDRESS = "123 Main St, Giddings, TX 78942"
INITIALS = ("JB", "MB", "RS", "LS")   # Buyer1, Buyer2, Seller1, Seller2
_TMP = tempfile.mkdtemp(prefix="trec_25_17_")

# Spec section 6, fixture 2 (clean two-buyer/two-seller), plus the blanks a
# spec 4.5 review would otherwise flag on it (6A expense, 6D days).
CLEAN = {
    "seller_name": "Robert Seller and Linda Seller",
    "buyer_name": "John Buyer and Mary Buyer",
    "county": "Lee",
    "legal_desc_1": "Abstract 12, 40.0 acres out of the J. Smith Survey",
    "address": ADDRESS,
    "cash_3a": "100,000.00",
    "tpf_3b_box": True,
    "financing_3b": "400,000.00",
    "sales_price_3c": "500,000.00",
    "adj_will_not": True,
    "escrow_agent": "Lee County Title",
    "earnest_money": "5,000.00",
    "option_fee": "200.00",
    "option_days": "10",
    "title_exp_seller": True,
    "title_company": "Lee County Title",
    "survey_1": True,
    "survey_1_days": "10",
    "survey_1_new_seller": True,
    "objection_days": "10",
    "tadd_is_not": True,
    "sdn_1": True,
    "asis_1": True,
    **{f"disc{n}_not": True for n in range(1, 9)},
    "water_1": True,
    "closing_date": "December 15",
    "closing_year": "26",
    "poss_closing": True,
    "p22_tpf": True,
    "eff_day": "20",
    "eff_month": "October",
    "eff_year": "26",
    "rcpt_option": "200.00",
    "rcpt_em": "5,000.00",
    "n_buyer_email": "john.buyer@example.com",
    "n_seller_email": "robert.seller@example.com",
    "n_buyer_agent_email": "buyer.agent@example.com",
    "n_seller_agent_email": "listing.agent@example.com",
}


def _initials_boxes(page):
    """The footer quad, left to right, by position (spec 2b)."""
    boxes = []
    for w in page.widgets():
        r = w.rect
        bottom = page.rect.height - r.y1
        if (w.field_type == fitz.PDF_WIDGET_TYPE_TEXT and tc_audit.INITIALS_X_RANGE[0] <= r.x0 <= tc_audit.INITIALS_X_RANGE[1]
                and bottom < tc_audit.INITIALS_MAX_BOTTOM):
            boxes.append((r.x0, w))
    return [w for _, w in sorted(boxes, key=lambda b: b[0])]


def _set(w, value):
    if w.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX:
        w.field_value = w.on_state() if value else "Off"
    else:
        w.field_value = value
    w.update()


def build(name="clean", **changes):
    values = {**CLEAN, **changes}
    by_name = {}
    for role, (page_no, field, _rect) in trec_25_17.MAP.items():
        if role in values:
            by_name[field] = values[role]
    headers = {f"header_p{p}": n for p, n in trec_25_17.HEADER_FIELDS}
    for key, field in headers.items():
        by_name[field] = values.get(key, ADDRESS)
    doc = fitz.open(BLANK)
    for page_no, page in enumerate(doc, 1):
        for w in page.widgets():
            if w.field_name in by_name:
                _set(w, by_name[w.field_name])
        if page_no <= 9:
            quad = _initials_boxes(page)
            assert len(quad) == 4, (page_no, len(quad))
            for slot, (w, default) in zip(("buyer1", "buyer2", "seller1", "seller2"), zip(quad, INITIALS)):
                _set(w, values.get(f"initials_p{page_no}_{slot}", default))
    path = os.path.join(_TMP, f"25-17_{name}.pdf")
    doc.save(path)
    return path


def build_40_11(name="clean", principal="400,000.00"):
    """The 40-11 that goes with the clean 25-17: principal = 3B, same parties."""
    set_to = {FA_FIELDS["first_loan_amount"]: principal, FA_FIELDS["address_p1"]: ADDRESS,
              FA_FIELDS["address_p2"]: ADDRESS}
    _, b1, b2, s1, s2 = tc_audit.FA_INITIALS_PAGE
    set_to.update(dict(zip((b1, b2, s1, s2), INITIALS)))
    doc = fitz.open(FA_SOURCE)
    for page in doc:
        for w in page.widgets():
            if w.field_name in set_to:
                _set(w, set_to[w.field_name])
    path = os.path.join(_TMP, f"40-11_{name}.pdf")
    doc.save(path)
    return path


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    for src in (build(), build_40_11()):
        dst = os.path.join(out, "trec_" + os.path.basename(src).replace("_clean", "_clean_two_parties"))
        os.replace(src, dst)
        print(dst)
