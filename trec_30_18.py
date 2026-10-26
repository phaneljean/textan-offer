"""
trec_30_18.py -- TC Check rules for TREC 30-18 Residential Condominium
Contract (Resale), 05-04-2026.

Every rule and field comes from txo-trec-qa's spec,
~/txtanoffer-team/work/txo-trec-qa/spec_30-18.md (2026-10-25), and its
prototype (spec_30-18_prototype/), reviewed and integrated here.

Never reuse pdf_filler.FIELD_MAP here (spec 2c). On a 30-18:
  - "1 PARTIES The parties to this contract are" is the BUYER (20-19: Seller)
  - "20" is the 9A closing DATE (20-19: the closing year); the year is "or within 7 days"
  - "20_2" is the Effective Date MONTH (20-19: the year)
  - "undefined_3" is the 2A County (20-19: 3A cash portion)
  - "Seller Financing Addendum" is the 3B dollar TEXT field
  - 4A/4B "boxes" are TEXT widgets ("4.B", "4.B58"): any non-blank text is checked

MAP is spec 2a's table, role -> (printed page, /T name, /Rect in PDF
points), on TREC's fillable pdf-forms/30-18_1.pdf (sha256
d5ff87810abebd8d..., 256 widgets). tests/test_30_18.py re-checks every row.
The form-agnostic parts (readability, 40-11/41-3/39-11 cross-checks, the
unreadable result) are trec_25_17's, called with this form's map.
"""
import re
from datetime import date

from pypdf import PdfReader

import filecrypt
import tc_audit as A
import trec_25_17 as F
from pdf_validator import _read_values

FORM = "TREC 30-18"
MAP = {
    'seller_name': (1, 'RESIDENTIAL CONDOMINIUM CONTRACT RESALE', (286.2, 705.5, 512.7, 717.2)),
    'buyer_name': (1, '1 PARTIES The parties to this contract are', (89.5, 695.8, 437.4, 707.5)),
    'unit': (1, 'A The Condominium Unit improvements and accessories described below are collectively', (257.8, 647.7, 329.9, 658.1)),
    'building': (1, 'undefined', (408.6, 647.6, 552.6, 658.8)),
    'condo_name': (1, '1 CONDOMINIUM', (115.9, 638.1, 376.8, 649.3)),
    'address': (1, 'of 1', (102.7, 628.5, 548.3, 639.5)),
    'address_2': (1, 'of 2', (102.7, 619.4, 548.3, 629.7)),
    'city': (1, 'undefined_2', (240.0, 611.1, 398.3, 620.5)),
    'county': (1, 'undefined_3', (454.0, 610.8, 548.3, 620.9)),
    'address_3': (1, 'addresszip code City of', (102.7, 601.3, 548.3, 612.5)),
    'parking_1': (1, 'Elements which have been specifically assigned to the Unit in any other manner Parking', (260.4, 547.9, 552.8, 558.5)),
    'parking_2': (1, 'areas assigned to the Unit are', (104.1, 537.7, 553.1, 548.9)),
    'exclusions_1': (1, '4 EXCLUSIONS The following improvements and accessories will be retained by Seller and', (350.6, 374.2, 556.8, 384.4)),
    'exclusions_2': (1, 'must be removed prior to delivery of possession', (102.7, 365.5, 552.2, 376.7)),
    'docs_1': (1, '1 Buyer has received a copy of the Documents Buyer is advised to read the Documents', (66.9, 336.0, 77.9, 345.9)),
    'docs_2': (1, '2 Buyer has not received a copy of the Documents Seller at Sellers expense shall deliver', (66.7, 315.4, 78.7, 325.4)),
    'docs_days': (1, 'may cancel the contract before the sixth day after Buyer receives the Documents by hand', (262.1, 304.9, 305.3, 314.5)),
    'cert_1': (1, '1 Buyer has received the Certificate', (67.4, 203.8, 79.0, 213.8)),
    'cert_2': (1, '2 Buyer has not received the Certificate Seller shall deliver the Certificate to Buyer within', (66.8, 194.2, 78.9, 203.1)),
    'cert_days': (1, 'before the sixth day after the date Buyer receives the Certificate by handdelivering or', (102.7, 181.9, 146.1, 191.4)),
    'cert_3': (1, '3 Buyer', (67.3, 137.6, 77.7, 147.0)),
    'rofr_days': (1, 'buy', (83.7, 43.7, 128.2, 53.8)),
    'cash_3a': (2, 'undefined_6', (438.5, 717.2, 558.1, 727.9)),
    'tpf_3b_box': (2, 'Third Party Financing Addendum', (320.2, 687.2, 330.8, 696.0)),
    'la_3b_box': (2, 'Check Box21', (81.6, 675.2, 92.2, 684.2)),
    'sf_3b_box': (2, 'Loan Assumption Addendum', (254.4, 675.2, 265.1, 684.0)),
    'financing_3b': (2, 'Seller Financing Addendum', (438.5, 674.4, 558.1, 685.2)),
    'sales_price_3c': (2, 'undefined_7', (438.5, 662.1, 558.1, 674.0)),
    'lease_4a_res': (2, '4.B', (55.1, 611.5, 65.8, 622.6)),
    'lease_4b_fixture': (2, '4.B58', (54.5, 591.2, 65.1, 602.3)),
    'escrow_agent': (2, 'undefined_8', (192.6, 539.2, 398.6, 552.3)),
    'escrow_addr_1': (2, 'undefined_9', (500.3, 539.5, 558.5, 550.6)),
    'escrow_addr_2': (2, 'address pg 2', (81.2, 529.0, 338.5, 541.4)),
    'earnest_money': (2, 'as earnest money to', (402.9, 529.1, 500.6, 539.3)),
    'option_fee': (2, 'as escrow agent  at', (147.3, 517.9, 236.5, 528.3)),
    'addl_em_days': (2, 'within', (131.9, 488.6, 170.2, 499.4)),
    'addl_em': (2, 'Buyer shall deliver additional earnest money of', (369.2, 497.4, 466.8, 507.9)),
    'option_days': (2, 'within 2', (81.0, 343.9, 119.3, 354.7)),
    'title_exp_seller': (2, 'Sellers', (313.9, 208.2, 325.4, 218.2)),
    'title_exp_buyer': (2, 'Buyers expense an owner policy of', (371.7, 208.2, 382.7, 217.6)),
    'title_company': (2, 'title insurance Title Policy issued by', (272.3, 197.5, 473.9, 207.3)),
    'prohibited_use': (3, 'following use or activity', (208.9, 612.4, 553.8, 622.6)),
    'objection_days': (3, 'Buyer must object the earlier of i the Closing Date or ii', (384.2, 591.2, 422.2, 601.9)),
    'sdn_1': (4, '1 Buyer has received the Notice', (60.2, 561.1, 71.7, 571.0)),
    'sdn_2': (4, '2 Buyer has not received the Notice Within', (60.2, 551.9, 71.7, 560.7)),
    'sdn_days': (4, 'B SELLERS DISCLOSURE NOTICE PURSUANT TO 5008 TEXAS PROPERTY CODE Notice', (401.4, 550.4, 436.8, 563.3)),
    'sdn_3': (4, '3The Texas Property Code does not require this Seller to furnish the Notice', (58.5, 485.7, 70.1, 495.1)),
    'asis_1': (4, '1 Buyer accepts the Property As Is', (59.5, 382.9, 70.2, 391.7)),
    'asis_2': (4, '2 Buyer accepts the Property As Is provided Seller at Sellers expense shall complete the', (59.5, 373.2, 70.2, 382.0)),
    'repairs_1': (4, 'undefined_15', (301.2, 361.4, 552.1, 372.3)),
    'repairs_2': (4, 'following specific repairs and treatments', (92.6, 350.9, 551.3, 361.8)),
    'rsc_amount': (4, 'Execeeding', (424.5, 92.5, 521.3, 104.0)),
    'p8_disclose_1': (5, 'contract Seller shall reimburse Buyer at closing for the cost of the residential service contract', (115.4, 698.4, 553.5, 709.7)),
    'closing_date': (5, '20', (298.1, 678.0, 404.9, 690.0)),
    'closing_year': (5, 'or within 7 days', (426.3, 678.4, 445.7, 689.3)),
    'poss_closing': (5, 'upon closing and funding', (336.5, 508.6, 348.0, 517.5)),
    'poss_lease': (5, 'according to a', (478.3, 508.6, 489.8, 517.5)),
    'special_prov_1': (5, 'Text2-4', (344.5, 345.1, 557.3, 356.1)),
    'special_prov_2': (5, 'Text2-3', (59.0, 334.8, 558.2, 345.5)),
    'special_prov_3': (5, 'Text2', (58.9, 325.7, 555.7, 335.6)),
    'seller_concession': (5, 'contract Seller shall reimburse Buyer at closing for the cost of the residential service contract2', (250.7, 260.8, 335.7, 270.4)),
    'assoc_fee_cap': (5, 'other charges resulting from the transfer of the Property not to exceed', (340.4, 145.9, 435.2, 157.9)),
    'b12_1_row': (5, 'dollar amount checkbox50', (60.2, 53.1, 71.3, 61.9)),
    'b12_1_dollar_box': (5, 'dollar amount checkbox53', (284.7, 53.0, 296.2, 61.9)),
    'b12_1_dollar': (5, '(b) dollar amount52', (310.3, 51.1, 367.8, 61.9)),
    'b12_1_pct_box': (5, 'percentage amount checkbox55', (388.5, 53.0, 400.0, 61.9)),
    'b12_1_pct': (5, '(b) percentage54', (408.3, 51.5, 445.9, 62.6)),
    'b12_2_row': (6, 'dollar amount checkbox51', (60.2, 730.8, 71.8, 739.6)),
    'b12_2_dollar_box': (6, 'dollar amount checkbox', (285.5, 730.7, 297.0, 739.6)),
    'b12_2_dollar': (6, '(b) dollar amount', (311.1, 728.8, 368.6, 739.6)),
    'b12_2_pct_box': (6, 'percentage amount checkbox', (389.3, 730.7, 400.8, 739.6)),
    'b12_2_pct': (6, '(b) percentage', (409.1, 729.2, 446.7, 740.4)),
    'n_buyer_email': (7, 'Text16', (111.3, 541.7, 291.1, 555.2)),
    'n_buyer_email_2': (7, 'Email', (62.5, 528.9, 290.8, 540.9)),
    'n_buyer_agent_email': (7, 'following specific repairs and treatments-12', (97.0, 463.4, 291.2, 475.9)),
    'n_seller_email': (7, 'Email_2', (373.2, 542.4, 543.9, 555.3)),
    'n_seller_email_2': (7, '2', (327.5, 528.4, 543.3, 542.5)),
    'n_seller_agent_email': (7, 'following specific repairs and treatments-13', (362.8, 463.8, 544.3, 475.8)),
    'p22_tpf': (7, 'Environmental Assessment Threatened or', (62.1, 410.1, 74.2, 419.5)),
    'p22_sale_other': (7, 'Addendum for Property Located Seaward', (61.6, 398.6, 73.1, 407.4)),
    'p22_appraisal': (7, 'Addendum for Release of Liability on', (62.1, 385.6, 73.6, 395.0)),
    'p22_seller_fin': (7, 'Check Box23', (62.9, 372.1, 72.9, 382.6)),
    'p22_1031': (7, 'Addendum for Property in a Propane Gas14', (61.9, 359.8, 73.4, 370.3)),
    'p22_short_sale': (7, 'Addendum for Property in a Propane Gas 15', (62.1, 346.8, 73.6, 357.3)),
    'p22_loan_assumption': (7, 'Other list16', (62.1, 336.5, 73.6, 345.9)),
    'p22_release_va': (7, 'Check Box22', (63.3, 324.0, 72.9, 334.0)),
    'p22_res_leases': (7, 'Check Box24', (62.9, 291.7, 72.9, 301.7)),
    'p22_fixture_leases': (7, 'PID', (61.9, 280.5, 73.4, 289.9)),
    'p22_buyer_temp': (7, 'Check Box26', (62.4, 267.9, 72.9, 277.9)),
    'p22_seller_temp': (7, 'Other (List)', (62.0, 257.1, 73.5, 266.5)),
    'p22_hydrostatic': (7, 'Check Box28', (62.9, 231.5, 72.9, 241.5)),
    'p22_environmental': (7, 'Other list 17', (62.2, 220.2, 73.8, 229.6)),
    'p22_lead_paint': (7, 'Check Box30', (62.9, 195.6, 72.9, 205.6)),
    'p22_propane': (7, 'Check Box32', (62.9, 173.9, 72.9, 183.4)),
    'p22_seaward': (7, 'Check Box33 18', (62.9, 162.5, 72.9, 172.5)),
    'p22_coastal': (7, 'Check Box34', (62.9, 151.9, 72.4, 161.3)),
    'p22_district_notices': (7, 'Check Box3520', (62.9, 139.5, 72.9, 148.9)),
    'p22_district_list_1': (7, 'System Service Area22', (283.1, 128.4, 550.3, 138.1)),
    'p22_non_realty': (7, 'Check Box33', (63.5, 72.9, 73.5, 82.9)),
    'p22_backup': (7, 'Check Box3456', (63.5, 60.3, 73.0, 69.7)),
    'p22_other': (7, 'Check Box35', (63.5, 48.8, 73.5, 58.3)),
    'p22_other_text': (7, 'System Service Area', (118.3, 47.7, 549.4, 60.2)),
    'eff_day': (8, 'BROKER FILL IN THE DATE OF FINAL ACCEPTANCE', (134.5, 494.6, 175.8, 509.2)),
    'eff_month': (8, '20_2', (214.5, 494.6, 409.7, 508.2)),
    'eff_year': (8, 'Effective Date', (431.3, 494.1, 465.0, 508.7)),
    'rcpt_option': (10, 'is acknowledged', (110.4, 689.1, 224.9, 702.9)),
    'rcpt_em': (10, 'is acknowledged_2', (112.8, 600.6, 222.0, 613.4)),
    'rcpt_addl_em': (10, 'is acknowledged_3', (112.8, 340.0, 222.0, 354.3)),
}
HEADER_FIELDS = [(2, 'Contract Concerning'), (3, 'Contract Concerning_2'), (4, 'Contract Concerning_3'), (5, 'Contract Concerning_4'), (6, 'Contract Concerning_5'), (7, 'Address of Property'), (8, 'Address of Property_2'), (9, 'Address of Property_3'), (10, 'Address of Property 10')]
HEADER_RECT = (118, 745, 414, 763)   # header widgets sit at y 746.9-759.0, x 120-415; "Page N of 10" starts at x ~414
TEXT_BOXES = {"lease_4a_res", "lease_4b_fixture"}   # text widgets used as checkboxes on TREC's fillable 30-18

NOTICE_EMAILS = [("Buyer's notice email (Paragraph 21)", "n_buyer_email"),
                 ("Seller's notice email (Paragraph 21)", "n_seller_email"),
                 ("Buyer's agent email (Paragraph 21 copy)", "n_buyer_agent_email"),
                 ("Seller's agent email (Paragraph 21 copy)", "n_seller_agent_email")]
INITIALS_PAGES = range(1, 8)   # footer quads on pages 1-7 only (spec 2a)
CORE_DRAFT = ["address", "county", "escrow_agent", "earnest_money", "option_fee", "title_company"]

# Unit designators: "123 Main St Unit 12", "#12", "Apt 12", "Ste 12", "No. 12", "Bldg C"
_UNIT_RE = re.compile(r"(?:\b(?:UNIT|APT|APARTMENT|STE|SUITE|NO|NUM|NUMBER)\b\.?|#)\s*#?\s*([A-Z0-9-]+)", re.I)
_BLDG_RE = re.compile(r"\b(?:BLDG|BUILDING)\b\.?\s*([A-Z0-9-]+)", re.I)


def _split_unit(addr: str):
    """('123 MAIN ST', '12') -- street via tc_audit._street with unit/bldg tokens removed."""
    text = addr or ""
    m = _UNIT_RE.search(text)
    unit = m.group(1).upper().lstrip("#") if m else None
    text = _BLDG_RE.sub(" ", _UNIT_RE.sub(" ", text))
    return A._street(text), unit


def contract_pages(reader) -> dict:
    """{printed page: page}. 30-18 page 1 prints 'Page    of 10' with NO number
    (pypdf, verified 2026-10-25), so the first 30-18 page without a number is page 1."""
    out = {}
    for page in reader.pages:
        text = page.extract_text() or ""
        if not re.search(r"TREC\s*NO\.?\s*30\s*-\s*18", text):
            continue
        m = re.search(r"Page\s*(\d+)\s*of\s*10", text)
        n = int(m.group(1)) if m else (1 if 1 not in out and re.search(r"Page\s*of\s*10", text) else None)
        if n is not None and n not in out:
            out[n] = page
    return out


class _Form(F._Form):
    """trec_25_17's role reader on this form's MAP, plus the two 30-18 quirks:
    4A/4B are text widgets (pdf_validator._is_checked would call the " "
    PyMuPDF writes for blank "checked"), and 2A(1)'s address is two lines."""
    def __init__(self, values, pages):
        super().__init__(values, pages, MAP)

    def on(self, role):
        if role in TEXT_BOXES:
            return self.values.get(self.map[role][1], "").strip() not in ("", "Off", "/Off")
        return super().on(role)

    def address(self):
        return " ".join(x for x in (self.v("address"), self.v("address_2")) if x)


def _issues(f: _Form) -> tuple:
    issues = []

    def add(sev, msg, key):
        issues.append({"severity": sev, "message": msg, "key": key})

    def one_of(label, roles, none_key=None):
        checked = [lab for lab, role in roles if f.on(role)]
        if len(checked) > 1:
            add("blocker", f"{label}: {' and '.join(checked)} are both checked -- only one box is allowed", "check_one_conflict")
        elif not checked and none_key:
            add("warning", f"{label}: no box is checked", none_key)
        return checked

    core_missing = 0
    # --- Paragraph 1 (30-18 roles: Seller idx 0, Buyer idx 1) ---------------
    if not f.v("buyer_name"):
        add("warning", "Paragraph 1: Buyer legal name is blank", "buyer_name")
    if not f.v("seller_name"):
        add("warning", "Paragraph 1: Seller legal name is blank", "seller_name")

    # --- Paragraph 2A(1) condominium unit ----------------------------------
    addr = f.address()
    if not addr:
        core_missing += 1
        add("blocker", "Paragraph 2A(1): the condominium's address (\"located at ___ (address/zip code)\") is blank", "address")
    if not f.v("county"):
        core_missing += 1
        add("blocker", "Paragraph 2A(1): County is blank -- title will kick back the file without this", "county")
    if not f.v("city"):
        add("warning", "Paragraph 2A(1): City is blank. If the condominium isn't inside a city's limits, this is "
                       "expected; otherwise fill it in", "condo_city_blank")
    if not f.v("unit") and not _split_unit(addr)[1]:
        add("warning", "Paragraph 2A(1): the Unit number is blank, and the address doesn't show one either. The form "
                       "identifies the Property as \"Unit ___ ... described in the Condominium Declaration\"",
            "condo_unit_blank")
    if not f.v("condo_name"):
        add("warning", "Paragraph 2A(1): the condominium project's name (\"of ___, a condominium project\") is blank",
            "condo_project_blank")

    # --- Paragraph 2B Documents, 2C Resale Certificate ---------------------
    if not one_of("Paragraph 2B (condominium Documents)", [("2B(1)", "docs_1"), ("2B(2)", "docs_2")]):
        add("blocker", "Paragraph 2B (Declaration, Bylaws and Rules): no box is checked -- the form says check one box "
                       "only: 2B(1) Buyer has received the Documents, or 2B(2) Seller will deliver them", "condo_docs_box_missing")
    if f.on("docs_2") and not f.v("docs_days"):
        add("blocker", "Paragraph 2B(2): the number of days for Seller to deliver the Documents is blank -- Buyer's "
                       "7-day termination right runs from delivery", "condo_docs_days_blank")
    if not one_of("Paragraph 2C (Resale Certificate)", [("2C(1)", "cert_1"), ("2C(2)", "cert_2"), ("2C(3)", "cert_3")]):
        add("blocker", "Paragraph 2C (Resale Certificate): no box is checked -- the form says check one box only: "
                       "2C(1) received, 2C(2) Seller will deliver it, or 2C(3) the Association didn't provide it",
            "resale_cert_box_missing")
    if f.on("cert_2") and not f.v("cert_days"):
        add("blocker", "Paragraph 2C(2): the number of days for Seller to deliver the Resale Certificate is blank -- "
                       "Buyer's 7-day termination right runs from delivery", "resale_cert_days_blank")

    # --- Paragraph 5A / 6A blanks, Effective Date --------------------------
    for role, msg, sev, key in (
            ("escrow_agent", "Paragraph 5A: Escrow Agent name is blank", "blocker", "escrow_agent_name"),
            ("earnest_money", "Paragraph 5A: Earnest money amount is blank", "blocker", "earnest_money_amount"),
            ("option_fee", "Paragraph 5A: option fee is blank -- under 5D, no option fee means no termination option. "
                           "Confirm that's intended.", "warning", "option_fee_amount"),
            ("title_company", "Paragraph 6A: Title Company is blank", "blocker", "title_company")):
        if not f.v(role):
            add(sev, msg, key)
            core_missing += 1
    if not (f.v("eff_day") and f.v("eff_month") and f.v("eff_year")):
        add("blocker", "Page 8 of 10: Effective Date is blank", "effective_date")

    # --- Paragraph 3 --------------------------------------------------------
    cash, loan, price = f.money("cash_3a"), f.money("financing_3b"), f.money("sales_price_3c")
    if price is not None and cash is not None:
        total = cash + (loan or 0)
        if abs(total - price) > 0.5:
            add("blocker", f"Paragraph 3: Cash portion (3A) {A._fmt_money(cash)} + financing (3B) {A._fmt_money(loan or 0)} = "
                           f"{A._fmt_money(total)}, but the Sales Price (3C) says {A._fmt_money(price)}", "sales_price_math")
    if loan and cash is None:
        add("warning", "Paragraph 3A: Cash portion is blank while 3B financing is filled in -- enter the cash "
                       "amount (or 0 for 100% financing)", "cash_portion_blank")

    # --- Paragraph 4 leases ("Check all applicable boxes") -----------------
    for box, p22, label in (("lease_4a_res", "p22_res_leases", "4A Residential Leases"),
                            ("lease_4b_fixture", "p22_fixture_leases", "4B Fixture Leases")):
        if f.on(box) and not f.on(p22):
            add("warning", f"Paragraph {label} is checked, and the form says that addendum is attached to this "
                           f"contract, but it isn't listed in Paragraph 22", "lease_addendum_not_listed")

    # --- 5B, 6A, 6C ---------------------------------------------------------
    if f.v("option_fee") and not f.slot("option_days"):
        add("blocker", "Paragraph 5B: Option fee is entered but the option period (number of days) is blank", "option_days_blank")
    one_of("Paragraph 6A (title policy expense)", [("Seller's", "title_exp_seller"), ("Buyer's", "title_exp_buyer")],
           none_key="title_expense_blank")
    if not f.v("objection_days"):
        add("warning", "Paragraph 6C: the number of days for Buyer to object is blank -- Buyer must object by the "
                       "earlier of the Closing Date or ___ days after receiving the Commitment and Exception Documents",
            "objection_days_blank")

    # --- Paragraph 7 --------------------------------------------------------
    if not one_of("Paragraph 7B (Seller's Disclosure Notice)", [("7B(1)", "sdn_1"), ("7B(2)", "sdn_2"), ("7B(3)", "sdn_3")]):
        add("blocker", "Paragraph 7B (Seller's Disclosure Notice): no box is checked -- one of 7B(1), 7B(2) or 7B(3) is required",
            "disclosure_box_missing")
    if f.on("sdn_2") and not f.v("sdn_days"):
        add("blocker", "Paragraph 7B(2): Seller's Disclosure delivery days are blank", "disclosure_days_blank")
    one_of("Paragraph 7D (Acceptance of condition)", [("7D(1)", "asis_1"), ("7D(2)", "asis_2")])

    # --- 9A -----------------------------------------------------------------
    closing, closing_year = f.slot("closing_date"), f.slot("closing_year")
    if closing:
        problem = A._closing_date_problem(closing, closing_year)
        if problem:
            add("blocker", problem, "closing_date_invalid")
    md, year, eff = A._parse_month_day(closing), A._year(closing_year), f.effective_date()
    if closing and md and year and eff:
        try:
            close_on = date(year, *md)
        except ValueError:
            close_on = None
        if close_on and close_on < eff:
            add("blocker", f"Paragraph 9A: Closing date ({close_on:%B %-d, %Y}) is before the Effective Date "
                           f"({eff:%B %-d, %Y})", "closing_before_effective")

    # --- 10A ----------------------------------------------------------------
    one_of("Paragraph 10A (Possession)", [("upon closing and funding", "poss_closing"),
                                         ("temporary residential lease", "poss_lease")])
    if f.on("poss_lease") and not (f.on("p22_buyer_temp") or f.on("p22_seller_temp")):
        add("warning", "Paragraph 10A: possession is by a temporary residential lease, but neither TREC temporary lease "
                       "is listed in Paragraph 22 (another written lease is allowed -- confirm one is attached)",
            "possession_lease_not_listed")

    # --- 12A(3) association transfer charges cap ----------------------------
    if not f.v("assoc_fee_cap"):
        add("warning", "Paragraph 12A(3): the cap on Association fees, deposits, reserves and other transfer charges "
                       "Buyer pays (\"not to exceed $___ and Seller shall pay any excess\") is blank", "assoc_fee_cap_blank")

    # --- 12B ----------------------------------------------------------------
    for label, row, d_box, d_amt, p_box, p_amt in (
            ("12B(1) (Seller pays toward Buyer's broker)", "b12_1_row", "b12_1_dollar_box", "b12_1_dollar", "b12_1_pct_box", "b12_1_pct"),
            ("12B(2) (Buyer pays toward Seller's broker)", "b12_2_row", "b12_2_dollar_box", "b12_2_dollar", "b12_2_pct_box", "b12_2_pct")):
        if not f.on(row):
            continue
        d_on, p_on = f.on(d_box), f.on(p_box)
        if not d_on and not p_on:
            add("blocker", f"Paragraph {label}: checked, but neither the $ box nor the % box is", "broker_contribution_incomplete")
        elif d_on and p_on:
            add("blocker", f"Paragraph {label}: both the $ box and the % box are checked -- only one is allowed", "broker_contribution_incomplete")
        elif d_on and not f.v(d_amt):
            add("blocker", f"Paragraph {label}: $ box checked but the amount is blank", "broker_contribution_incomplete")
        elif p_on and not f.v(p_amt):
            add("blocker", f"Paragraph {label}: % box checked but the percentage is blank", "broker_contribution_incomplete")

    # --- Receipts (page 10) vs 5A -------------------------------------------
    for label, receipt, role, what in (("Option Fee receipt (page 10)", "rcpt_option", "option_fee", "option fee in Paragraph 5A"),
                                       ("Earnest Money receipt (page 10)", "rcpt_em", "earnest_money", "earnest money in Paragraph 5A")):
        got, want = f.money(receipt), f.money(role)
        if got is not None and want is not None and abs(got - want) > 0.005:
            add("blocker", f"{label} says {A._fmt_money(got)}, but the {what} is {A._fmt_money(want)}", "receipt_mismatch")

    # --- Headers, pages 2-10 (unit-aware) -----------------------------------
    street, unit = _split_unit(addr)
    unit = unit or (f.v("unit").upper() or None)
    if street:
        for page_no, name in HEADER_FIELDS:
            if page_no not in f.pages:
                continue
            header = f.values.get(name, "").strip() or A._text_in_rect(f.pages[page_no], HEADER_RECT)
            if not header:
                add("warning", f"Page {page_no} of 10: \"Address of Property\" header is blank", "header_address_blank")
                continue
            h_street, h_unit = _split_unit(header)
            if h_street != street:
                if header[:1].isdigit():
                    add("blocker", f'Page {page_no} of 10: header address reads "{header}" but Paragraph 2A(1) says '
                                   f'"{addr}"', "header_address")
                else:
                    add("warning", f'Page {page_no} of 10: the header reads "{header}" and Paragraph 2A(1)\'s address is '
                                   f'"{addr}". Confirm the header identifies this property.', "header_address_differs")
            elif h_unit and unit and h_unit != unit:
                add("blocker", f'Page {page_no} of 10: header shows unit {h_unit} but Paragraph 2A(1) is Unit {unit}',
                    "header_address")

    # --- Paragraph 11 --------------------------------------------------------
    provisions = " ".join(f.v(r) for r in ("special_prov_1", "special_prov_2", "special_prov_3")).strip()
    if A._BUSINESS_TERM_RE.search(provisions):
        snippet = provisions if len(provisions) <= 90 else provisions[:87] + "..."
        add("warning", f'Paragraph 11 (Special Provisions) reads like a business term ("{snippet}"). Paragraph 11 is for '
                       f"informational items only; licensees can't draft business terms there -- use a TREC addendum or "
                       f"have a party or attorney draft it", "special_provisions_business_term")

    if f.on("p22_appraisal") and not f.on("p22_tpf"):
        add("warning", "Paragraph 22 lists the Addendum Concerning Right to Terminate Due to Lender's Appraisal, but not the "
                       "Third Party Financing Addendum -- TREC 49-1 is for use only when the Third Party Financing "
                       "Addendum is attached", "appraisal_without_tpf")
    for label, role in NOTICE_EMAILS:
        val = f.v(role)
        if val and not A.EMAIL_RE.match(val):
            add("warning", f'{label} "{val}" is not a valid email address -- notices sent there will bounce', "email_invalid")
    return issues, core_missing


# TREC 30-17 (11-04-2024) Effective Date, printed page 8 of 10: day, MONTH
# ("20_2"), year. Used only to word the outdated-form finding. NOTE: TREC no
# longer serves 30-17 (pdf-forms/30-17.pdf is 404), so these rects come from
# an archive.org (Wayback) copy of it, not from TREC: web.archive.org/web/
# 2025id_/.../pdf-forms/30-17.pdf, sha256 f1ba0916..., PDF title "30-17",
# author "TREC", render-verified by txo-trec-qa 2026-10-25. Name AND rect
# must both match, so a different file can only read as blank (must-fix).
EFFECTIVE_30_17 = {
    "day": ("BROKER FILL IN THE DATE OF FINAL ACCEPTANCE", (132.5, 455.4, 173.8, 470.0)),
    "month": ("20_2", (212.6, 455.4, 407.7, 469.0)),
    "year": ("Effective Date", (429.3, 454.9, 463.0, 469.5)),
}


def effective_date_30_17(path):
    for page in PdfReader(filecrypt.pdf_source(path)).pages:
        text = page.extract_text() or ""
        if not (re.search(r"TREC\s*NO\.?\s*30\s*-\s*17", text) and re.search(r"Page\s*8\s*of\s*10", text)):
            continue
        got = {}
        for name, value, _ft, rect in A._widget_rows(page):
            for part, (want, want_rect) in EFFECTIVE_30_17.items():
                if name == want and all(abs(a - b) <= 1.0 for a, b in zip(rect, want_rect)):
                    got[part] = value.strip()
        day = re.sub(r"\D", "", got.get("day", ""))
        md = A._parse_month_day(f"{got.get('month', '')} {day}")
        year = A._year(got.get("year", ""))
        try:
            return date(year, *md) if md and year else None
        except ValueError:
            return None
    return None


def check_30_18(pdf_paths, detections, page_count) -> dict:
    """tc_audit's built-in checker for "30-18". Same result shape as
    check_tc_file's 20-19 result, plus "form"."""
    main_index = next(i for i, det in enumerate(detections) if any(fid == "30-18" for fid, _ in det))
    main_path = pdf_paths[main_index]
    all_values = [_read_values(p) for p in pdf_paths]
    values = all_values[main_index]
    pages = contract_pages(PdfReader(filecrypt.pdf_source(main_path)))
    if not F.fields_readable(MAP, pages):
        return F.unreadable_result(FORM, page_count)
    f = _Form(values, pages)
    issues, core_missing = _issues(f)

    buyers, sellers = A._party_names(f.v("buyer_name")), A._party_names(f.v("seller_name"))
    footers = {n: page for n, page in f.pages.items() if n in INITIALS_PAGES}
    for page_label, buyer_boxes, seller_boxes in A._initials_by_position(footers):
        # _initials_by_position labels pages "of 12" (the 20-19's count).
        issues.extend(A._check_initials_pair(page_label.replace(" of 12", " of 10"), buyer_boxes, seller_boxes,
                                             buyers, sellers))

    # Spec 4.6: 36-11's title says "(NOT FOR USE WITH CONDOMINIUMS)". Any
    # page of any file stamped 36-N; that file isn't also "unrecognized".
    poa_ids = sorted({fid for det in detections for fid, _ in det if fid and fid.startswith("36-")})
    if poa_ids:
        issues.append({"severity": "warning",
                       "message": f"A TREC {poa_ids[0]} owners association addendum is attached. The current 36-11's "
                                  "title says \"(NOT FOR USE "
                                  "WITH CONDOMINIUMS)\"; on a 30-18, Paragraph 2 covers the condominium Documents and "
                                  "Resale Certificate, and 12A(3) the Association transfer charges",
                       "key": "poa_addendum_on_condo"})
    more, has_addendum, has_loan_assumption, has_amendment = F.attachment_issues(
        f, pdf_paths, detections, all_values, main_index, buyers, sellers, handled=("36-",))
    issues.extend(more)

    blocking = [i for i in issues if i["severity"] == "blocker"]
    unit = f.v("unit")
    return {
        "recognized": True,
        "form": FORM,
        "complete": not blocking,
        "issues": issues,
        "looks_like_blank_draft": core_missing / len(CORE_DRAFT) >= A.BLANK_DRAFT_THRESHOLD,
        "page_count": page_count,
        "has_addendum": has_addendum,
        "has_amendment": has_amendment,
        "has_loan_assumption": has_loan_assumption,
        "severity": A._severity_summary(issues),
        "property": {
            "address": f.address() + (f", Unit {unit}" if unit and not _split_unit(f.address())[1] else ""),
            "city": f.v("city"),
            "county": f.v("county"),
        },
    }
