"""Filled TREC 9-18 test files, built from TREC's own fillable 9-18_2.pdf
(tests/fixtures/trec_9-18_blank.pdf, sha256 b6e8542f94a698c2...) the way
txo-trec-qa's spec section 6 describes, by role from trec_9_18.MAP -- never
by guessing a /T name.

    python3 -m tests.trec_9_18_fixtures <out_dir>   # writes the clean pair to look at

build(**changes) returns a path. A change is role=value: a string for a
text field (" " = blank: PyMuPDF ignores an empty value), True/False for a
checkbox. "initials_p3_seller2" etc. set one initials box; "header_p4" one
page header.
"""
import os
import sys
import tempfile

import fitz

import tc_audit
import trec_9_18
from financing_addendum import FIELD_MAP as FA_FIELDS
from tests.trec_25_17_fixtures import FA_SOURCE, INITIALS, _initials_boxes, _set

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLANK = os.path.join(ROOT, "tests", "fixtures", "trec_9-18_blank.pdf")
ADDRESS = "123 Main St, Bastrop, TX 78602"
_TMP = tempfile.mkdtemp(prefix="trec_9_18_")

# Spec section 6, fixture 1 (clean two-buyer/two-seller), plus the blanks a
# spec 4.5 review would otherwise flag on it (6D days).
CLEAN = {
    "seller_name": "Robert Seller and Linda Seller",
    "buyer_name": "John Buyer and Mary Buyer",
    "lot": "7",
    "block": "B",
    "addition": "Pine Ridge",
    "city": "Bastrop",
    "county": "Bastrop",
    "address": ADDRESS,
    "cash_3a": "40,000.00",
    "tpf_3b_box": True,
    "financing_3b": "160,000.00",
    "sales_price_3c": "200,000.00",
    "adj_will_not": True,
    "nrl_is_not": True,
    "escrow_agent": "Bastrop Title",
    "earnest_money": "3,000.00",
    "option_fee": "200.00",
    "option_days": "10",
    "title_exp_seller": True,
    "title_company": "Bastrop Title",
    "shortages_i": True,
    "survey_2": True,
    "survey_2_days": "20",
    "objection_days": "10",
    "poa_is_not": True,
    "tadd_is_not": True,
    "asis_1": True,
    # Every 7E pair "is not"; 7E(7) wholly/partly (idx 89/90) left alone.
    **{f"disc{n}_not": True for n in range(1, 9)},
    "water_1": True,
    "closing_date": "December 15",
    "closing_year": "26",
    "p22_tpf": True,
    "eff_day": "20",
    "eff_month": "October",
    "eff_year": "26",
    "rcpt_option": "200.00",
    "rcpt_em": "3,000.00",
    "n_buyer_email": "john.buyer@example.com",
    "n_seller_email": "robert.seller@example.com",
    "n_buyer_agent_email": "buyer.agent@example.com",
    "n_seller_agent_email": "listing.agent@example.com",
}


def build(name="clean", **changes):
    values = {**CLEAN, **changes}
    by_name = {}
    for role, (page_no, field, _rect) in trec_9_18.MAP.items():
        if role in values:
            by_name[field] = values[role]
    for page_no, field in trec_9_18.HEADER_FIELDS:
        by_name[field] = values.get(f"header_p{page_no}", ADDRESS)
    doc = fitz.open(BLANK)
    for page_no, page in enumerate(doc, 1):
        for w in page.widgets():
            if w.field_name in by_name:
                _set(w, by_name[w.field_name])
        if page_no in trec_9_18.INITIALS_PAGES:
            quad = _initials_boxes(page)
            assert len(quad) == 4, (page_no, len(quad))
            for slot, (w, default) in zip(("buyer1", "buyer2", "seller1", "seller2"), zip(quad, INITIALS)):
                _set(w, values.get(f"initials_p{page_no}_{slot}", default))
    path = os.path.join(_TMP, f"9-18_{name}.pdf")
    doc.save(path)
    return path


def build_40_11(name="clean", principal="160,000.00"):
    """The 40-11 that goes with the clean 9-18: principal = 3B, same parties."""
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
    for src, dst in ((build(), "trec_9-18_clean_two_parties.pdf"), (build_40_11(), "trec_9-18_40-11_clean.pdf")):
        os.replace(src, os.path.join(out, dst))
        print(os.path.join(out, dst))
