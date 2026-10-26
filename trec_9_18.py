"""
trec_9_18.py -- TC Check rules for TREC 9-18 Unimproved Property Contract
(05-04-2026), the third TREC contract after the 20-19 and 25-17.

Every rule and field comes from txo-trec-qa's spec,
~/txtanoffer-team/work/txo-trec-qa/spec_9-18_25-17.md (2026-10-25):
section 2a (field map), 3 (which 20-19 rules carry over), 4 (9-18 rules),
7 (false-positive guards). Nothing here is guessed from a field name.

Never reuse pdf_filler.FIELD_MAP or trec_25_17.MAP on this form. The 9-18
shares 157 /T names with the 20-19 and many mean something else here
(spec 2c):
  - "undefined_3" is the 3C Sales Price (20-19: 3A cash portion)
  - "undefined_5" is the 5A option fee (20-19: Sales Price), which is how a
    clean 9-18 got "3A $200,000 + 3B ... but the Sales Price says $200"
  - "Third Party Financing Addendum" is the 3B CHECKBOX here (25-17: the 3B
    dollar field)
  - "Buyer" / "Seller" on page 2 are the 6A Seller's / Buyer's expense
    boxes -- swapped
  - "20_2" is the Effective Date YEAR (25-17: the month)

MAP below is spec 2a's table, role -> (printed page, /T name, /Rect in PDF
points), each row re-checked 2026-10-25 against TREC's fillable
pdf-forms/9-18_2.pdf (sha256 b6e8542f94a698c2..., 291 widgets): every name
is on exactly one widget, on that page, within 1pt of that rect.
tests/test_9_18.py re-checks it, so a typo fails a test instead of reading
the wrong blank.

The form-agnostic parts (reading by role, page footers, the address
header, 40-11/41-3/39-11 cross-checks, the unreadable result) are
trec_25_17's, called with this form's map and page count.
"""
import re
from datetime import date

from pypdf import PdfReader

import filecrypt
import tc_audit as A
import trec_25_17 as F
from pdf_validator import _read_values

FORM = "TREC 9-18"
PAGES = 11

MAP = {
    'seller_name': (1, '1 PARTIES The parties to this contract are', (278.8, 689.3, 520.3, 700.8)),
    'buyer_name': (1, 'and', (79.8, 677.8, 434.6, 689.9)),
    'lot': (1, '2 PROPERTY Lot', (145.6, 656.2, 290.8, 667.8)),
    'block': (1, 'Block', (328.1, 656.2, 557.3, 667.8)),
    'addition': (1, 'Addition', (57.1, 644.2, 515.3, 656.8)),
    'city': (1, 'City of', (93.2, 632.7, 297.7, 644.9)),
    'county': (1, 'County of', (355.8, 632.7, 556.9, 644.8)),
    'address': (1, 'Texas known as', (141.1, 622.2, 556.9, 633.3)),
    'cash_3a': (1, 'undefined', (468.4, 557.7, 556.9, 568.7)),
    'tpf_3b_box': (1, 'Third Party Financing Addendum', (315.4, 527.3, 324.8, 536.7)),
    'la_3b_box': (1, 'Check Box1', (75.8, 516.0, 85.3, 525.4)),
    'sf_3b_box': (1, 'Seller Financing Addendum', (245.9, 514.8, 254.7, 524.2)),
    'financing_3b': (1, 'undefined_2', (468.4, 513.4, 556.9, 524.4)),
    'sales_price_3c': (1, 'undefined_3', (468.4, 501.0, 558.2, 512.0)),
    'adj_will': (1, 'Sellers', (154.7, 490.3, 163.6, 499.7)),
    'adj_will_not': (1, 'Buyers expense an owners policy of', (189.6, 490.3, 199.5, 499.7)),
    'adj_acres': (1, 'acres', (73.5, 468.4, 122.6, 479.4)),
    'adj_per_acre': (1, '$ per acre', (79.4, 449.2, 132.0, 459.1)),
    'adj_term_days': (1, 'within days', (392.2, 429.2, 418.8, 440.8)),
    'adj_to_3a': (1, '3a', (389.3, 413.6, 398.2, 423.0)),
    'adj_to_3b': (1, '3b', (420.2, 413.6, 430.1, 423.0)),
    'adj_prop': (1, 'proportinately to 3a and 3b', (455.8, 413.6, 465.7, 423.0)),
    'nrl_is': (1, 'Seller is', (231.0, 330.3, 240.9, 339.7)),
    'nrl_is_not': (1, 'Seller is not', (258.6, 330.3, 268.5, 339.7)),
    'nrl_1': (1, 'i will not be amended or deleted from the title policy or', (58.2, 307.7, 68.1, 317.1)),
    'nrl_2': (1, 'ii will be amended to read shortages in area at the expense of', (58.2, 292.8, 68.1, 302.8)),
    'nrl_days': (1, 'within the time required Seller may terminate this contract or exercise Sellers remedies under', (333.7, 270.1, 371.9, 281.0)),
    'escrow_agent': (1, 'as earnest money to', (154.3, 227.0, 404.9, 238.5)),
    'escrow_addr_1': (1, 'the other party in writing before entering into a contract of sale Disclose if applicable', (500.5, 225.6, 558.8, 237.7)),
    'escrow_addr_2': (1, 'undefined_4', (74.4, 216.8, 382.2, 228.0)),
    'earnest_money': (1, 'earnest money of', (446.0, 216.5, 558.7, 228.1)),
    'option_fee': (1, 'undefined_5', (197.1, 206.2, 292.5, 217.1)),
    'addl_em': (1, 'agent at', (340.1, 174.1, 441.1, 184.6)),
    'addl_em_days': (1, '3 days', (93.1, 163.1, 129.1, 174.0)),
    'option_days': (2, '4 days', (75.7, 719.8, 113.9, 730.7)),
    'title_exp_seller': (2, 'Buyer', (310.8, 571.3, 321.8, 581.3)),
    'title_exp_buyer': (2, 'Seller', (362.2, 571.3, 372.1, 580.7)),
    'title_company': (2, 'title insurance Title Policy issued by', (260.9, 559.1, 559.2, 571.0)),
    'shortages_i': (2, 'i 1', (70.3, 404.5, 80.2, 413.9)),
    'shortages_ii': (2, 'ii 2', (70.3, 392.6, 80.2, 402.5)),
    'shortages_buyer': (2, 'is', (430.6, 393.2, 441.3, 402.5)),
    'shortages_seller': (2, 'is not subject to', (479.5, 393.2, 488.6, 402.5)),
    'survey_1': (2, '1 Within', (57.4, 252.4, 67.3, 261.3)),
    'survey_1_days': (2, 'Title Company Sellers existing survey of the Property and a Residential Real Property Affidavit', (125.2, 249.6, 157.8, 259.4)),
    'survey_1_new_seller': (2, 'Sellers_2', (90.1, 183.6, 100.6, 193.0)),
    'survey_1_new_buyer': (2, 'Buyers expense no later than 3 days prior to Closing Date', (145.2, 183.6, 155.7, 193.6)),
    'survey_2': (2, '2 Within', (58.9, 173.9, 68.9, 183.3)),
    'survey_2_days': (2, 'at Buyers expense Buyer is deemed to receive the survey on the date of actual receipt or', (124.1, 171.1, 158.8, 181.9)),
    'survey_3': (2, '3 Within', (58.9, 123.5, 68.4, 134.0)),
    'survey_3_days': (2, 'furnish a new survey to Buyer', (125.5, 123.0, 160.2, 133.2)),
    'prohibited_use': (2, 'undefined_8', (71.5, 55.2, 555.5, 67.2)),
    'objection_days': (2, 'Buyer must object the earlier of i the Closing Date or ii', (369.4, 45.2, 404.3, 56.6)),
    'poa_is': (3, 'Texas Agricultural Development District For additional information contact the Texas1', (455.4, 554.1, 466.4, 563.5)),
    'poa_is_not': (3, 'is_2', (480.6, 554.1, 490.0, 563.5)),
    'tadd_is': (4, 'is no. 1', (411.5, 707.7, 422.5, 717.1)),
    'tadd_is_not': (4, 'is not 2', (448.7, 707.7, 458.0, 717.1)),
    'asis_1': (4, '1 Buyer accepts the Property As Is', (60.8, 411.8, 71.0, 420.6)),
    'asis_2': (4, '2 Buyer accepts the Property As Is provided Seller at Sellers expense shall complete the', (60.8, 401.1, 71.0, 410.4)),
    'repairs_1': (4, 'undefined_12-2', (304.0, 386.9, 563.1, 399.0)),
    'repairs_2': (4, 'undefined_12-3', (91.1, 377.5, 557.8, 388.3)),
    'disc1_is': (4, 'Texas Agricultural Development District For additional information contact the Texas-4', (124.1, 202.4, 135.1, 211.8)),
    'disc1_not': (4, 'is_2-4', (159.2, 202.4, 168.6, 211.8)),
    'disc2_is': (4, 'Texas Agricultural Development District For additional information contact the Texas-5', (124.1, 186.4, 135.1, 195.8)),
    'disc2_not': (4, 'is_2-5', (159.2, 186.4, 168.6, 195.8)),
    'disc3_is': (4, 'Texas Agricultural Development District For additional information contact the Texas-6', (125.1, 168.4, 136.1, 177.8)),
    'disc3_not': (4, 'is_2-6', (160.2, 168.4, 169.6, 177.8)),
    'disc4_is': (4, 'Texas Agricultural Development District For additional information contact the Texas-7', (125.1, 151.4, 136.1, 160.8)),
    'disc4_not': (4, 'is_2-7', (160.2, 151.4, 169.6, 160.8)),
    'disc5_is': (4, 'Texas Agricultural Development District For additional information contact the Texas-8', (124.1, 131.4, 135.1, 140.8)),
    'disc5_not': (4, 'is_2-8', (160.2, 131.8, 169.6, 140.8)),
    'disc6_is': (4, 'Texas Agricultural Development District For additional information contact the Texas-1', (124.4, 113.8, 135.4, 123.2)),
    'disc6_not': (4, '-1', (159.6, 113.8, 169.0, 123.2)),
    'disc7_is': (4, 'Texas Agricultural Development District For additional information contact the Texas', (124.4, 94.8, 135.4, 104.2)),
    'disc7_not': (4, 'is_2-2', (159.6, 94.8, 169.0, 104.2)),
    'disc7_wholly': (4, 'Texas Agricultural Development District For additional information contact the Texas-7a', (384.1, 95.4, 395.1, 104.8)),
    'disc7_partly': (4, 'is_2-7b', (435.2, 95.4, 444.6, 104.8)),
    'disc8_is': (4, 'Texas Agricultural Development District For additional information contact the Texas-3', (124.4, 84.8, 135.4, 94.2)),
    'disc8_not': (4, 'is_2-3', (159.6, 84.8, 169.0, 94.2)),
    'disc_explain_1': (4, 'following specific repairs and treatments', (536.5, 73.3, 550.8, 84.8)),
    'disc_explain_2': (4, 'undefined_12', (74.5, 63.8, 550.9, 74.5)),
    'water_1': (5, 'Check percentage2', (65.3, 700.3, 74.7, 709.7)),
    'water_2': (5, 'Check percentage', (65.9, 687.6, 75.3, 697.0)),
    'water_3': (5, 'Check dollar amount', (65.1, 620.8, 76.1, 630.2)),
    'water_provider_1': (5, 'Disclose-1', (308.3, 513.7, 550.6, 524.6)),
    'water_provider_2': (5, 'Disclose 2-2', (115.1, 504.2, 549.5, 515.1)),
    'p8_disclose_1': (5, 'Disclose', (485.7, 445.5, 555.2, 457.1)),
    'p8_disclose_2': (5, 'Disclose 2', (65.3, 434.7, 554.4, 446.1)),
    'closing_date': (5, 'A The closing of the sale will be on or before', (294.2, 414.6, 424.6, 427.9)),
    'closing_year': (5, '20', (444.4, 414.1, 472.6, 427.2)),
    'special_prov_1': (5, 'Text2-4', (351.7, 183.2, 555.8, 193.7)),
    'special_prov_2': (5, 'Text2-3', (63.5, 173.8, 554.5, 184.0)),
    'seller_concession': (5, 'escrow fee and other expenses payable by Seller under this contract', (259.0, 105.0, 325.5, 117.3)),
    'b12_1_row': (6, 'Buyer dollar amount3', (62.3, 617.9, 73.3, 627.8)),
    'b12_1_dollar_box': (6, 'Buyer dollar amount4', (286.9, 618.2, 297.8, 628.1)),
    'b12_1_dollar': (6, 'dollar amount5', (312.1, 616.3, 372.5, 628.0)),
    'b12_1_pct_box': (6, 'Buyer percentage6', (389.6, 617.8, 400.6, 628.3)),
    'b12_1_pct': (6, '% of sales price7', (402.4, 616.1, 446.1, 628.1)),
    'b12_2_row': (6, 'Buyer dollar amount', (61.7, 595.2, 72.7, 605.2)),
    'b12_2_dollar_box': (6, 'Buyer dollar amount9', (287.3, 594.6, 298.3, 604.5)),
    'b12_2_dollar': (6, 'dollar amount', (311.7, 593.5, 371.6, 605.2)),
    'b12_2_pct_box': (6, 'Buyer percentage', (390.1, 594.2, 401.1, 604.6)),
    'b12_2_pct': (6, '% of sales price', (402.8, 594.5, 447.0, 606.5)),
    'n_buyer_addr': (7, 'when mailed to handdelivered at or transmitted by fax or electronic transmission as follows', (107.1, 356.7, 299.8, 370.1)),
    'n_buyer_phone': (7, 'Text4', (111.8, 293.5, 299.5, 308.6)),
    'n_buyer_email': (7, 'undefined_17-2', (108.5, 230.6, 298.9, 246.3)),
    'n_buyer_email_2': (7, 'undefined_17', (63.5, 199.7, 299.1, 215.4)),
    'n_buyer_agent_addr': (7, 'undefined_21', (105.5, 143.7, 297.4, 158.0)),
    'n_buyer_agent_email': (7, 'Email', (95.7, 49.3, 300.2, 63.6)),
    'n_seller_addr': (7, 'undefined_16', (372.5, 357.1, 561.2, 369.5)),
    'n_seller_email': (7, 'undefined_18', (375.0, 230.3, 562.1, 245.1)),
    'n_seller_email_2': (7, 'undefined_18-1', (328.3, 199.8, 562.3, 214.1)),
    'n_seller_agent_addr': (7, 'undefined_22', (372.4, 144.3, 560.2, 158.5)),
    'n_seller_agent_email': (7, 'Email_2', (360.1, 49.5, 561.5, 62.4)),
    'p22_tpf': (8, 'Addendum for Property Located Seaward', (61.7, 654.1, 72.1, 663.5)),
    'p22_sale_other': (8, 'Addendum for Property Subject to', (61.4, 640.2, 72.9, 649.1)),
    'p22_appraisal': (8, 'Addendum for BackUp Contract', (62.0, 629.0, 72.9, 638.4)),
    'p22_seller_fin': (8, 'Addendum for Property in a Propane Gas', (61.7, 615.7, 72.2, 625.7)),
    'p22_1031': (8, 'Addendum Concerning Right to', (61.4, 602.0, 72.9, 611.4)),
    'p22_short_sale': (8, 'Section 1031 Exchange', (61.7, 590.3, 73.2, 600.2)),
    'p22_loan_assumption': (8, 'Third Party Financing Addendum_2', (62.8, 577.3, 74.3, 586.7)),
    'p22_release_va': (8, 'Seller Financing Addendum_2', (62.8, 563.3, 74.3, 572.1)),
    'p22_res_leases': (8, 'Addendum for Coastal Area Property', (62.6, 523.4, 73.6, 533.3)),
    'p22_fixture_leases': (8, 'Environmental Assessment Threatened or', (62.1, 512.5, 74.1, 522.4)),
    'p22_buyer_temp': (8, 'Buyers Temporary Residential Lease', (61.8, 499.5, 73.3, 509.5)),
    'p22_seller_temp': (8, 'Sellers Temporary Residential Lease', (61.8, 485.5, 73.3, 495.5)),
    'p22_hydrostatic': (8, 'Addendum for Reservation of Oil Gas', (62.4, 456.4, 73.3, 466.9)),
    'p22_environmental': (8, 'Addendum for Sale of Other Property by32', (62.6, 444.2, 72.8, 454.4)),
    'p22_propane': (8, 'PID', (62.9, 417.8, 74.4, 427.2)),
    'p22_seaward': (8, 'Other list14', (63.7, 403.7, 75.2, 413.6)),
    'p22_coastal': (8, 'PID15', (62.8, 391.5, 74.4, 400.9)),
    'p22_district_notices': (8, 'Other list16', (63.6, 377.4, 75.2, 387.3)),
    'p22_district_list_1': (8, 'City_3', (256.7, 366.9, 569.0, 381.3)),
    'p22_poa': (8, 'Addendum for Sale of Other Property by', (62.2, 304.0, 73.1, 313.5)),
    'p22_non_realty': (8, 'PID17', (62.5, 292.9, 74.0, 302.3)),
    'p22_backup': (8, 'Other list18', (63.3, 278.8, 74.8, 288.7)),
    'p22_mineral_reservation': (8, 'PID19', (62.5, 266.6, 74.0, 276.0)),
    'p22_other': (8, 'Other list', (63.3, 250.4, 74.8, 262.4)),
    'p22_other_text': (8, 'Zip_320', (117.3, 252.1, 568.9, 266.4)),
    'eff_day': (9, 'EXECUTED the', (133.8, 545.6, 176.2, 558.9)),
    'eff_month': (9, 'day of', (215.2, 545.4, 408.6, 559.3)),
    'eff_year': (9, '20_2', (430.2, 545.6, 465.0, 559.4)),
    'rcpt_option': (11, 'is acknowledged', (106.5, 691.1, 205.0, 704.1)),
    'rcpt_em': (11, 'is acknowledged_2', (105.8, 610.9, 204.8, 623.3)),
    'rcpt_addl_em': (11, 'is acknowledged_3', (112.4, 364.0, 203.8, 379.6)),
}

# Page header "Contract Concerning ___ (Address of Property)", pages 2-11
# (spec 2a). Page 1 has none. The printed "Page N of 11" starts at x 452.5
# and "Concerning" ends before x 126, so the rect holds only the blank.
HEADER_FIELDS = [(2, "Contract Concerning"), (3, "Contract Concerning_2"), (4, "Contract Concerning_3"),
                 (5, "Page 5 of 9"), (6, "Page 6 of 9"), (7, "Contract Concerning_4"), (8, "Contract Address 8"),
                 (9, "Address of Property"), (10, "Address of Property_2"), (11, "Address of Property_60")]
HEADER_RECT = (127, 745, 450, 766)
# Initials footers are on pages 1-8 only, found by position (x0
# 211/253/347/396, p7-p8 214/256/350/399, bottom ~20pt): the /T names lie
# (page 5's Buyer1 box is "then to Buyers Expenses and any excess refunded
# to Buyer"). tc_audit._initials_by_position's x-range and bottom fit.
INITIALS_PAGES = range(1, 9)

# Paragraph 21 line-1 email fields only; line 2 can hold anything (spec 3).
NOTICE_EMAILS = [("Buyer's notice email (Paragraph 21)", "n_buyer_email"),
                 ("Seller's notice email (Paragraph 21)", "n_seller_email"),
                 ("Buyer's agent email (Paragraph 21 copy)", "n_buyer_agent_email"),
                 ("Seller's agent email (Paragraph 21 copy)", "n_seller_agent_email")]

CORE_DRAFT = F.CORE_DRAFT


def _issues(f) -> tuple:
    """(issues, core_missing) for the contract itself."""
    issues = []

    def add(sev, msg, key):
        issues.append({"severity": sev, "message": msg, "key": key})

    def one_of(label, roles, none_key=None):
        """A 'check one' group: more than one checked is a must-fix. With
        none_key, none checked is a review (spec 4.5); the groups where none
        is a must-fix (4B, 7F) say so themselves below."""
        checked = [lab for lab, role in roles if f.on(role)]
        if len(checked) > 1:
            add("blocker", f"{label}: {' and '.join(checked)} are both checked -- only one box is allowed", "check_one_conflict")
        elif not checked and none_key:
            add("warning", f"{label}: no box is checked", none_key)
        return checked

    core_missing = 0

    # --- Paragraph 2: property description (spec 4.1) -----------------------
    # "Lot ___, Block ___, ___ Addition, City of ___, County of ___, Texas,
    # known as ___ (address/zip code), or as described on attached exhibit".
    # No City rule: land outside a city leaves "City of" blank (spec 3). A
    # blank address alone is no finding.
    described = any(f.v(r) for r in ("lot", "block", "addition", "address"))
    if not described:
        core_missing += 1
        add("warning", 'Paragraph 2: no lot, block, addition or address is filled in. The form allows "as described '
                       'on attached exhibit"; confirm the exhibit is attached.', "property_description_blank")
    elif not f.v("county"):
        # Suppressed when the whole description is blank (exhibit).
        add("blocker", "Paragraph 2: County is blank -- title will kick back the file without this", "county")
    if not f.v("county"):
        core_missing += 1

    # --- Paragraph 1 parties (Seller idx 0, Buyer idx 1, as on the 20-19) --
    if not f.v("buyer_name"):
        add("warning", "Paragraph 1: Buyer legal name is blank", "buyer_name")
    if not f.v("seller_name"):
        add("warning", "Paragraph 1: Seller legal name is blank", "seller_name")

    # --- Paragraph 5A / 6A blanks ------------------------------------------
    for role, msg, sev, key in (
            ("escrow_agent", "Paragraph 5A: Escrow Agent name is blank", "blocker", "escrow_agent_name"),
            ("earnest_money", "Paragraph 5A: Earnest money amount is blank", "blocker", "earnest_money_amount"),
            ("option_fee", "Paragraph 5A: option fee is blank -- under 5D, no option fee means no termination option. "
                           "Confirm that's intended.", "warning", "option_fee_amount"),
            ("title_company", "Paragraph 6A: Title Company is blank", "blocker", "title_company")):
        if not f.v(role):
            add(sev, msg, key)
            core_missing += 1

    # --- Effective Date (page 9) -------------------------------------------
    if not (f.v("eff_day") and f.v("eff_month") and f.v("eff_year")):
        add("blocker", f"Page 9 of {PAGES}: Effective Date is blank", "effective_date")

    # --- Paragraph 3 -------------------------------------------------------
    cash, loan, price = f.money("cash_3a"), f.money("financing_3b"), f.money("sales_price_3c")
    if price is not None and cash is not None:
        total = cash + (loan or 0)
        if abs(total - price) > 0.5:
            add("blocker", f"Paragraph 3: Cash portion (3A) {A._fmt_money(cash)} + financing (3B) {A._fmt_money(loan or 0)} = "
                           f"{A._fmt_money(total)}, but the Sales Price (3C) says {A._fmt_money(price)}", "sales_price_math")
    if loan and cash is None:
        add("warning", "Paragraph 3A: Cash portion is blank while 3B financing is filled in -- enter the cash "
                       "amount (or 0 for 100% financing)", "cash_portion_blank")

    # --- Paragraph 3D acreage adjustment (spec 4.2, same wording as 25-17).
    # 9-18 6C has no "no survey is required" option, so no no-survey rule.
    will = one_of("Paragraph 3D (Sales Price adjustment)", [("will", "adj_will"), ("will not", "adj_will_not")])
    adj_to = one_of("Paragraph 3D (adjustment applied to)",
                    [("3A", "adj_to_3a"), ("3B", "adj_to_3b"), ("proportionately to 3A and 3B", "adj_prop")])
    acres_filled = f.v("adj_acres") or f.v("adj_per_acre")
    if will == ["will"]:
        if not (f.v("adj_acres") and f.v("adj_per_acre")):
            add("blocker", "Paragraph 3D: the Sales Price \"will\" be adjusted, but the acres or the $ per acre is blank "
                           "-- the adjustment the parties chose can't be applied", "acreage_adjustment_incomplete")
        if not adj_to:
            add("warning", "Paragraph 3D: the Sales Price \"will\" be adjusted, but none of 3A, 3B or proportionately "
                           "is checked", "acreage_adjustment_review")
        if not f.v("adj_term_days"):
            add("warning", "Paragraph 3D: the Sales Price \"will\" be adjusted, but the number of days to terminate "
                           "after an adjustment of more than 10% is blank", "acreage_adjustment_review")
    elif will != ["will", "will not"] and acres_filled:
        add("warning", "Paragraph 3D says the Sales Price will not be adjusted (or neither box is checked), but the "
                       "acres or per-acre amount is filled in", "acreage_adjustment_conflict")

    # --- Paragraph 4B Natural Resource Leases (spec 4.3, 9-18) --------------
    # "Seller [ ] is [ ] is not a party to a Natural Resource Lease. If
    # Seller is a party to a Natural Resource Lease, check one of the
    # following: (1) ... (2) ..."
    party = one_of("Paragraph 4B (Natural Resource Lease)", [("is", "nrl_is"), ("is not", "nrl_is_not")])
    nrl = one_of("Paragraph 4B(1)/(2) (Natural Resource Leases delivered)", [("4B(1)", "nrl_1"), ("4B(2)", "nrl_2")])
    if party == ["is"] and not nrl:
        add("blocker", "Paragraph 4B: Seller is a party to a Natural Resource Lease, but neither 4B(1) nor 4B(2) is "
                       "checked -- the form says check one of them", "nrl_option_missing")
    if party == ["is not"] and nrl:
        add("warning", "Paragraph 4B says Seller is not a party to a Natural Resource Lease, but 4B(1)/(2) is checked",
            "lease_conflict")
    if f.on("nrl_2") and not f.v("nrl_days"):
        add("warning", "Paragraph 4B(2): the number of days for Buyer to terminate is blank", "lease_incomplete")

    # --- Paragraph 5B option days ------------------------------------------
    if f.v("option_fee") and not f.slot("option_days"):
        add("blocker", "Paragraph 5B: Option fee is entered but the option period (number of days) is blank", "option_days_blank")

    # --- Paragraph 6 title and survey (spec 4.5) ---------------------------
    one_of("Paragraph 6A (title policy expense)", [("Seller's", "title_exp_seller"), ("Buyer's", "title_exp_buyer")],
           none_key="title_expense_blank")
    short = one_of("Paragraph 6A(8) (shortages in area)", [("(i)", "shortages_i"), ("(ii)", "shortages_ii")])
    if short == ["(ii)"]:
        payer = [lab for lab, r in (("Buyer", "shortages_buyer"), ("Seller", "shortages_seller")) if f.on(r)]
        if len(payer) != 1:
            add("blocker", "Paragraph 6A(8)(ii): \"at the expense of [ ] Buyer [ ] Seller\" -- "
                           + ("both are checked" if payer else "neither is checked"), "check_one_conflict")
    survey = one_of("Paragraph 6C (Survey)", [("6C(1)", "survey_1"), ("6C(2)", "survey_2"), ("6C(3)", "survey_3")],
                    none_key="survey_box_missing")
    if len(survey) == 1:
        n = survey[0][3]
        if not f.v(f"survey_{n}_days"):
            add("blocker", f"Paragraph {survey[0]}: the number of days is blank", "survey_days_blank")
    if survey == ["6C(1)"]:
        one_of("Paragraph 6C(1) (new survey expense)", [("Seller's", "survey_1_new_seller"), ("Buyer's", "survey_1_new_buyer")],
               none_key="survey_expense_blank")
    if not f.v("objection_days"):
        add("warning", "Paragraph 6D: the number of days for Buyer to object is blank -- Buyer must object by the "
                       "earlier of the Closing Date or ___ days after receiving the title documents", "objection_days_blank")
    one_of("Paragraph 6E(2) (Owners association)", [("is", "poa_is"), ("is not", "poa_is_not")])
    if f.on("poa_is") and not f.on("p22_poa"):
        add("warning", "Paragraph 6E(2) says the property IS subject to an owners association, but the Owners Association "
                       "Addendum isn't checked in Paragraph 22 -- attach it or correct 6E(2)", "poa_addendum_missing")
    one_of("Paragraph 6E(8) (Texas Agricultural Development District)", [("is", "tadd_is"), ("is not", "tadd_is_not")])

    # --- Paragraph 7 property condition ------------------------------------
    # 9-18 has no Seller's Disclosure Notice paragraph: its 7B is As Is, and
    # none checked is no finding (spec 3, planted #31).
    one_of("Paragraph 7B (Acceptance of condition)", [("7B(1)", "asis_1"), ("7B(2)", "asis_2")])
    aware = False
    for n in range(1, 9):
        if one_of(f"Paragraph 7E({n})", [("is", f"disc{n}_is"), ("is not", f"disc{n}_not")]) == ["is"]:
            aware = True
    part = one_of("Paragraph 7E(7)", [("wholly", "disc7_wholly"), ("partly", "disc7_partly")])
    if f.on("disc7_is") and not f.on("disc7_not") and not part:
        add("warning", "Paragraph 7E(7): \"is\" is checked, but neither \"wholly\" nor \"partly\" is", "disclosure_incomplete")
    if aware and not (f.v("disc_explain_1") or f.v("disc_explain_2")):
        add("warning", "Paragraph 7E: Seller is aware of an item above, but the explanation line is blank -- the form "
                       "says to explain (it may be on an attached sheet)", "disclosure_explain_blank")
    water = one_of("Paragraph 7F (Seller's Water Disclosure)", [("7F(1)", "water_1"), ("7F(2)", "water_2"), ("7F(3)", "water_3")])
    if not water:
        # Must-fix, Phanel approved 2026-10-25 (spec 7 risk 10).
        add("blocker", "Paragraph 7F (Seller's Water Disclosure): no box is checked -- the form says check one box only, "
                       "and 7F(3) covers a Seller who doesn't have to deliver it", "water_disclosure_box_missing")
    # 7F(2) "Within ___ days" has no form field on the 9-18 (spec 2a), so
    # there's no days-blank rule here, unlike 25-17 7K(2).
    if f.on("water_3") and not (f.v("water_provider_1") or f.v("water_provider_2")):
        add("warning", "Paragraph 7F(3)(e): the water provider isn't named", "water_provider_blank")

    # --- 9A closing date ---------------------------------------------------
    closing = f.slot("closing_date")
    closing_year = f.slot("closing_year")
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
    # Paragraph 10 has no boxes on the 9-18 (spec 3): no possession check.

    # --- 12B broker contributions ------------------------------------------
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

    # --- Receipts on page 11 vs 5A -----------------------------------------
    for label, receipt, role, what in (("Option Fee receipt (page 11)", "rcpt_option", "option_fee", "option fee in Paragraph 5A"),
                                       ("Earnest Money receipt (page 11)", "rcpt_em", "earnest_money", "earnest money in Paragraph 5A")):
        got, want = f.money(receipt), f.money(role)
        if got is not None and want is not None and abs(got - want) > 0.005:
            add("blocker", f"{label} says {A._fmt_money(got)}, but the {what} is {A._fmt_money(want)}", "receipt_mismatch")

    # --- Address header, pages 2-11 (only when Paragraph 2's address is filled)
    issues.extend(F.header_issues(f, HEADER_FIELDS, HEADER_RECT, PAGES, "Paragraph 2"))

    # --- Paragraph 11 ------------------------------------------------------
    provisions = " ".join(f.v(r) for r in ("special_prov_1", "special_prov_2")).strip()
    hit = A._BUSINESS_TERM_RE.search(provisions)
    if hit:
        snippet = provisions if len(provisions) <= 90 else provisions[:87] + "..."
        add("warning", f'Paragraph 11 (Special Provisions) reads like a business term ("{snippet}"). Paragraph 11 is for '
                       f"informational items only; licensees can't draft business terms there -- use a TREC addendum or "
                       f"have a party or attorney draft it", "special_provisions_business_term")

    # --- Reservations without an addendum (spec 4.4) -----------------------
    # 9-18 Paragraph 2: "RESERVATIONS: Any reservation for oil, gas, or other
    # minerals, water, timber, or other interests is made in accordance with
    # an attached addendum." Same narrow regex as the 25-17.
    if not f.on("p22_mineral_reservation") and not f.on("p22_other") and not f.v("p22_other_text"):
        m = F._RESERVATION_RE.search(provisions)
        if m:
            snippet = provisions if len(provisions) <= 90 else provisions[:87] + "..."
            add("warning", f"Paragraph 2 says any reservation of minerals, water, timber or other interests is made by an "
                           f'attached addendum. Paragraph 11 mentions "{snippet}", but no reservation addendum is listed '
                           f"in Paragraph 22. If nothing is being reserved, this doesn't apply.", "reservation_without_addendum")

    # --- Lender's Appraisal (spec 4.9) -------------------------------------
    if f.on("p22_appraisal") and not f.on("p22_tpf"):
        add("warning", "Paragraph 22 lists the Addendum Concerning Right to Terminate Due to Lender's Appraisal, but not the "
                       "Third Party Financing Addendum -- TREC 49-1 is for use only when the Third Party Financing "
                       "Addendum is attached", "appraisal_without_tpf")

    # --- Notice emails -----------------------------------------------------
    for label, role in NOTICE_EMAILS:
        val = f.v(role)
        if val and not A.EMAIL_RE.match(val):
            add("warning", f'{label} "{val}" is not a valid email address -- notices sent there will bounce', "email_invalid")
    return issues, core_missing


def check_9_18(pdf_paths, detections, page_count) -> dict:
    """tc_audit's built-in checker for "9-18". Same result shape as
    check_tc_file's 20-19 result, plus "form"."""
    main_index = next(i for i, det in enumerate(detections) if any(fid == "9-18" for fid, _ in det))
    main_path = pdf_paths[main_index]
    all_values = [_read_values(p) for p in pdf_paths]
    values = all_values[main_index]
    pages = F.contract_pages(PdfReader(filecrypt.pdf_source(main_path)), "9-18", PAGES)
    if not F.fields_readable(MAP, pages):
        return F.unreadable_result(FORM, page_count)
    f = F._Form(values, pages, MAP)

    issues, core_missing = _issues(f)

    buyers, sellers = A._party_names(f.v("buyer_name")), A._party_names(f.v("seller_name"))
    footers = {n: page for n, page in f.pages.items() if n in INITIALS_PAGES}
    for page_label, buyer_boxes, seller_boxes in A._initials_by_position(footers):
        # _initials_by_position labels pages "of 12" (the 20-19's count).
        page_label = page_label.replace(" of 12", f" of {PAGES}")
        issues.extend(A._check_initials_pair(page_label, buyer_boxes, seller_boxes, buyers, sellers))

    more, has_addendum, has_loan_assumption, has_amendment = F.attachment_issues(
        f, pdf_paths, detections, all_values, main_index, buyers, sellers)
    issues.extend(more)

    blocking = [i for i in issues if i["severity"] == "blocker"]
    lot_block = ", ".join(x for x in (f.v("lot") and f"Lot {f.v('lot')}", f.v("block") and f"Block {f.v('block')}",
                                       f.v("addition") and f"{f.v('addition')} Addition") if x)
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
            # Land is often described by lot/block, not addressed.
            "address": f.v("address") or lot_block,
            "city": f.v("city"),
            "county": f.v("county"),
        },
    }
