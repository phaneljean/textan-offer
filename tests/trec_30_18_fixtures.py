"""Filled TREC 30-18 test files, built from TREC's own fillable 30-18_1.pdf
(tests/fixtures/trec_30-18_blank.pdf, sha256 d5ff87810abebd8d...) by role
from trec_30_18.MAP, per txo-trec-qa's spec_30-18.md section 6 (ported from
its prototype).

    python3 -m tests.trec_30_18_fixtures <out_dir>   # writes the clean pair to look at

build(**changes): " " = blank text, True/False = a box (4A/4B are text
widgets on this form: True writes "X"). "initials_p3_seller2" sets one
initials box; "header_p4" one page header.

Other fixtures: trec_30-17_blank.pdf is an archive.org copy of TREC's
fillable 30-17 (sha256 f1ba0916..., TREC itself now 404s it);
trec_30-16_flat.pdf is TREC's pdf-forms/30-16.pdf (010cbc83..., flat). The
36-11 is the repo's own 36-11.pdf template, byte-identical to TREC's.
"""
import os
import sys
import tempfile

import fitz

import tc_audit
import trec_30_18
from financing_addendum import FIELD_MAP as FA_FIELDS
from tests.trec_25_17_fixtures import FA_SOURCE, INITIALS, _initials_boxes
from tests.trec_25_17_fixtures import _set as _set_box

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLANK = os.path.join(ROOT, "tests", "fixtures", "trec_30-18_blank.pdf")
ADDRESS = "123 Main St, Unit 12, Austin, TX 78701"
HEADER = ADDRESS
_TMP = tempfile.mkdtemp(prefix="trec_30_18_")

CLEAN = {
    "seller_name": "Robert Seller and Linda Seller", "buyer_name": "John Buyer and Mary Buyer",
    "unit": "12", "building": "C", "condo_name": "Main Street Lofts", "address": "123 Main St, Unit 12, 78701",
    "city": "Austin", "county": "Travis", "parking_1": "Space P-12",
    "docs_2": True, "docs_days": "3", "cert_2": True, "cert_days": "3", "rofr_days": "10",
    "cash_3a": "45,000.00", "tpf_3b_box": True, "financing_3b": "380,000.00", "sales_price_3c": "425,000.00",
    "escrow_agent": "Lone Star Title", "escrow_addr_1": "100 Congress Ave", "escrow_addr_2": "Austin, TX 78701",
    "earnest_money": "5,000.00", "option_fee": "250.00", "option_days": "7",
    "title_exp_seller": True, "title_company": "Lone Star Title", "objection_days": "10",
    "sdn_1": True, "asis_1": True,
    "closing_date": "December 15", "closing_year": "26", "poss_closing": True,
    "assoc_fee_cap": "500.00", "p22_tpf": True,
    "eff_day": "20", "eff_month": "October", "eff_year": "26",
    "rcpt_option": "250.00", "rcpt_em": "5,000.00",
    "n_buyer_email": "john.buyer@example.com", "n_seller_email": "robert.seller@example.com",
    "n_buyer_agent_email": "buyer.agent@example.com", "n_seller_agent_email": "listing.agent@example.com",
}



def _set(w, value):
    if w.field_type != fitz.PDF_WIDGET_TYPE_CHECKBOX and isinstance(value, bool):
        value = "X" if value else " "   # 4A/4B text-widget "boxes"
    _set_box(w, value)


def build(name="clean", **changes):
    values = {**CLEAN, **changes}
    by_name = {field: values[role] for role, (_p, field, _r) in trec_30_18.MAP.items() if role in values}
    for page_no, field in trec_30_18.HEADER_FIELDS:
        by_name[field] = values.get(f"header_p{page_no}", HEADER)
    doc = fitz.open(BLANK)
    for page_no, page in enumerate(doc, 1):
        for w in page.widgets():
            if w.field_name in by_name:
                _set(w, by_name[w.field_name])
        if page_no in trec_30_18.INITIALS_PAGES:
            quad = _initials_boxes(page)
            assert len(quad) == 4, (page_no, len(quad))
            for slot, (w, default) in zip(("buyer1", "buyer2", "seller1", "seller2"), zip(quad, INITIALS)):
                _set(w, values.get(f"initials_p{page_no}_{slot}", default))
    path = os.path.join(_TMP, f"30-18_{name}.pdf")
    doc.save(path)
    return path


def build_40_11(name="clean", principal="380,000.00"):
    """The 40-11 that goes with the clean 30-18: principal = 3B, same parties."""
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


def build_30_17(name, day, month, year):
    """The 30-17 with only its page-8 Effective Date filled (day, MONTH '20_2', year)."""
    doc = fitz.open(os.path.join(ROOT, "tests", "fixtures", "trec_30-17_blank.pdf"))
    want = dict(zip(("BROKER FILL IN THE DATE OF FINAL ACCEPTANCE", "20_2", "Effective Date"), (day, month, year)))
    for w in doc[7].widgets():
        if w.field_name in want:
            _set(w, want[w.field_name])
    path = os.path.join(_TMP, f"30-17_{name}.pdf")
    doc.save(path)
    return path


def build_36_11(name="clean"):
    doc = fitz.open(os.path.join(ROOT, "36-11.pdf"))
    for w in doc[0].widgets():
        if w.field_name == "Street Address and City":
            _set(w, "123 Main St, Unit 12, Austin")
    path = os.path.join(_TMP, f"36-11_{name}.pdf")
    doc.save(path)
    return path


def strip_widgets(src, name):
    """Every widget deleted, the /AcroForm /Fields names left behind -- a
    partly flattened export (spec_30-18.md 0.2)."""
    doc = fitz.open(src)
    for page in doc:
        for w in list(page.widgets()):
            page.delete_widget(w)
    path = os.path.join(_TMP, f"stripped_{name}.pdf")
    doc.save(path)
    return path


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    for src, dst in ((build(), "trec_30-18_clean_two_parties.pdf"), (build_40_11(), "trec_30-18_40-11_clean.pdf")):
        os.replace(src, os.path.join(out, dst))
        print(os.path.join(out, dst))
