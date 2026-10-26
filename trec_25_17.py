"""
trec_25_17.py -- TC Check rules for TREC 25-17 Farm and Ranch Contract
(05-04-2026), the second form after the 20-19.

Every rule and field comes from txo-trec-qa's spec,
~/txtanoffer-team/work/txo-trec-qa/spec_9-18_25-17.md (2026-10-25):
section 2b (field map), 3 (which 20-19 rules carry over), 4 (25-17 rules),
7 (false-positive guards). Nothing here is guessed from a field name.

Never reuse pdf_filler.FIELD_MAP on this form. The 25-17 shares 150 /T
names with the 20-19 and many mean something else here (spec 2c):
  - "1 PARTIES The parties to this contract are" is the BUYER (20-19: Seller)
  - "20_2" is the Effective Date MONTH (20-19: the year)
  - "Third Party Financing Addendum" is the 3B dollar TEXT field, so a
    20-19 checkbox rule on it reads any amount as "checked"
  - "undefined_3" is the 3C Sales Price (20-19: 3A cash portion)
That's why a filled 25-17 got 7-8 false must-fixes before 2026-10-25.

MAP below is spec 2b's table, role -> (printed page, /T name, /Rect in PDF
points). tests/test_25_17.py checks every row's page and rect against
TREC's own 25-17_4.pdf, so a typo here fails a test instead of reading the
wrong blank. Rects are also used to read a value drawn on the page instead
of typed into the field (spec 7.7: closing date, option days, headers).
"""
import re
from datetime import date

from pypdf import PdfReader

import filecrypt
import tc_audit as A
import trec_forms
from amendment import FIELD_MAP as AMEND_FIELDS
from financing_addendum import FIELD_MAP as FA_FIELDS
from pdf_filler import FIELD_MAP as FIELD_MAP_20_19
from pdf_validator import _is_checked, _money_to_int, _read_values

FORM = "TREC 25-17"

MAP = {
    'seller_name': (1, 'FARM AND RANCH CONTRACT', (275.8, 712.1, 555.4, 724.7)),
    'buyer_name': (1, '1 PARTIES The parties to this contract are', (121.8, 702.0, 418.8, 714.0)),
    'county': (1, 'reservations are collectively referred to as the Property', (352.4, 659.3, 555.5, 671.2)),
    'county_2': (1, 'A LAND The land situated in the County of', (70.1, 648.2, 555.5, 661.1)),
    'legal_desc_1': (1, 'described as follows', (210.6, 635.8, 555.5, 649.1)),
    'legal_desc_2': (1, 'addresszip code together with all rights privileges and appurtenances pertaining thereto', (68.9, 624.2, 555.5, 636.3)),
    'legal_desc_3': (1, 'addresszip code together with all rights privileges and appurtenances pertaining thereto-2', (69.2, 612.0, 555.8, 625.3)),
    'address': (1, 'addresszip code together with all rights privileges and appurtenances pertaining thereto-3', (327.4, 601.9, 555.3, 612.7)),
    'acc_portable': (1, 'portable', (217.2, 454.4, 228.3, 464.2)),
    'acc_other': (1, 'other', (267.1, 434.4, 278.6, 443.8)),
    'acc_other_text': (1, 'fuel tanks', (314.2, 433.6, 554.9, 445.5)),
    'exclusions_1': (1, 'The following improvements accessories and crops will be retained by Seller', (316.5, 307.4, 557.3, 318.4)),
    'exclusions_2': (1, 'and must be removed prior to delivery of possession', (69.5, 297.8, 556.8, 308.7)),
    'cash_3a': (1, 'undefined_2', (426.0, 257.2, 551.6, 267.6)),
    'tpf_3b_box': (1, 'A Cash portion of Sales Price payable by Buyer at closing', (310.0, 228.2, 320.1, 237.0)),
    'la_3b_box': (1, 'Check Box27', (69.2, 217.2, 79.9, 227.3)),
    'sf_3b_box': (1, 'Seller Financing Addendum', (244.2, 217.3, 254.4, 226.1)),
    'financing_3b': (1, 'Third Party Financing Addendum', (425.5, 215.3, 551.1, 225.7)),
    'sales_price_3c': (1, 'undefined_3', (425.5, 204.2, 552.7, 214.5)),
    'adj_will': (1, 'will', (147.6, 194.2, 159.1, 204.1)),
    'adj_will_not': (1, 'C Sales Price Sum of A and B', (182.6, 194.2, 194.3, 204.1)),
    'adj_acres': (1, 'will not be adjusted based on the survey required by Paragraph 6C', (68.8, 174.3, 116.5, 185.0)),
    'adj_per_acre': (1, 'per acre If the Sales Price is adjusted by more than 10 either party may terminate this', (464.3, 164.7, 516.8, 174.7)),
    'adj_term_days': (1, 'which the license holder or the license holders spouse parent or child is a beneficiary to notify the-3', (258.8, 135.4, 284.3, 147.0)),
    'adj_to_3a': (1, '3A', (194.8, 118.6, 206.9, 128.0)),
    'adj_to_3b': (1, '3B', (226.9, 118.6, 238.9, 128.4)),
    'adj_prop': (1, 'proportionately to 3A and 3B', (263.5, 117.9, 274.5, 128.2)),
    'lease_4a_res': (1, 'Residential', (43.2, 72.5, 54.2, 81.1)),
    'lease_4b_fixture': (1, 'Fixture 1', (43.6, 50.0, 54.6, 59.8)),
    'lease_4c_nrl': (2, 'Natural Resources', (41.6, 728.2, 52.2, 738.1)),
    'nrl_1': (2, 'Seller has', (54.8, 697.8, 65.8, 707.6)),
    'nrl_2': (2, 'Seller has not', (54.8, 685.8, 65.8, 695.6)),
    'nrl_days': (2, 'which the license holder or the license holders spouse parent or child is a beneficiary to notify the', (298.8, 664.8, 331.0, 677.5)),
    'lease_4d_surface': (2, 'Fixture 1-3', (41.4, 644.3, 52.4, 654.2)),
    'surface_1': (2, 'Natural Resources-4', (56.4, 614.3, 67.4, 624.2)),
    'surface_2': (2, 'Seller has-6', (56.0, 600.9, 67.0, 610.7)),
    'surface_2_text': (2, 'The following improvements accessories and crops will be retained by Seller-5', (369.6, 589.3, 564.5, 600.0)),
    'surface_2_text_2': (2, 'and must be removed prior to delivery of possession-6', (86.9, 578.7, 564.9, 592.8)),
    'surface_3': (2, 'Seller has not-7', (56.0, 569.9, 67.0, 579.7)),
    'surface_days': (2, '3 days 2-5', (446.5, 538.0, 480.1, 549.3)),
    'escrow_agent': (2, 'other party in writing before entering into a contract of sale Disclose if applicable', (149.2, 483.6, 398.5, 495.7)),
    'escrow_addr_1': (2, '5 EARNEST', (495.6, 482.7, 563.0, 494.2)),
    'escrow_addr_2': (2, 'as earnest money to', (68.2, 472.4, 331.5, 484.1)),
    'earnest_money': (2, 'at', (394.2, 473.6, 509.1, 483.2)),
    'option_fee': (2, 'Text1', (136.8, 463.2, 245.1, 474.6)),
    'addl_em': (2, 'to escrow', (335.0, 444.9, 418.3, 457.6)),
    'addl_em_days': (2, '3 days 2', (536.5, 445.1, 569.7, 456.4)),
    'option_days': (2, '3 days 2-7', (69.5, 299.9, 115.1, 311.7)),
    'title_exp_seller': (2, 'Sellers', (303.9, 169.7, 315.4, 179.0)),
    'title_exp_buyer': (2, 'Buyers expense an owner policy of', (362.0, 169.7, 373.5, 179.5)),
    'title_company': (2, 'A TITLE POLICY Seller shall furnish to Buyer at', (265.8, 157.8, 484.8, 169.7)),
    'shortages_i': (2, 'i will not be amended or deleted from the title policy or', (70.5, 46.8, 82.0, 56.2)),
    'shortages_ii': (2, 'ii will be amended to read shortages in area at the expense of', (70.5, 36.7, 82.0, 46.1)),
    'shortages_buyer': (2, 'Buyer', (429.6, 36.7, 441.1, 46.1)),
    'shortages_seller': (2, 'Seller', (484.5, 36.7, 495.5, 46.1)),
    'survey_1': (3, '1 Within', (53.2, 592.6, 64.7, 601.9)),
    'survey_1_days': (3, 'the Title Company and Buyers lenders Check one box only', (120.5, 590.7, 161.2, 600.7)),
    'survey_1_new_seller': (3, 'Buyer_3', (517.1, 533.4, 528.1, 542.8)),
    'survey_1_new_buyer': (3, 'Seller no later than 3 days prior to Closing Date', (83.9, 523.4, 96.1, 532.8)),
    'survey_2': (3, '2 Within', (53.7, 513.5, 65.2, 523.3)),
    'survey_2_days': (3, 'expense of', (121.0, 512.8, 158.7, 523.2)),
    'survey_3': (3, '3 Within', (54.2, 465.5, 65.2, 475.4)),
    'survey_3_days': (3, 'the date specified in this paragraph whichever is earlier', (121.2, 463.8, 164.4, 473.6)),
    'survey_4': (3, '4 No survey is required', (53.7, 444.4, 65.2, 453.8)),
    'prohibited_use': (3, 'undefined_6', (429.5, 397.0, 566.7, 409.6)),
    'objection_days': (3, 'Commitment Exception Documents and the survey Buyers failure to object within the time', (366.2, 367.7, 406.4, 377.0)),
    'exc_doc_1': (3, 'Document 1', (69.8, 173.3, 297.9, 187.2)),
    'surface_exceptions_1': (3, 'The following Leases will be permitted exceptions in the Title Policy and will not be a basis for', (293.6, 115.3, 565.9, 127.3)),
    'surface_exceptions_2': (3, 'objection to title', (68.5, 104.3, 565.9, 115.7)),
    'tadd_is': (4, 'information', (425.4, 460.7, 437.4, 470.1)),
    'tadd_is_not': (4, 'is', (457.8, 460.7, 467.0, 469.5)),
    'sdn_1': (4, '1 Buyer has received the Notice', (57.8, 159.8, 69.3, 168.5)),
    'sdn_days': (4, 'contract Seller shall deliver the Notice to Buyer', (407.3, 147.2, 446.7, 159.3)),
    'sdn_2': (4, '2 Buyer has not received the Notice', (58.0, 148.7, 69.2, 158.1)),
    'sdn_3': (4, '3 The Texas Property Code does not require this Seller to furnish the Notice', (58.2, 80.7, 69.8, 90.8)),
    'asis_1': (5, '1 Buyer accepts the Property As Is', (64.5, 699.3, 75.2, 708.6)),
    'asis_2': (5, '2 Buyer accepts the Property As Is provided Seller at Sellers expense shall complete the', (64.3, 689.0, 75.0, 698.8)),
    'repairs_1': (5, 'undefined_11', (308.9, 678.9, 574.6, 688.9)),
    'repairs_2': (5, 'following specific repairs and treatments', (95.3, 669.0, 574.4, 680.2)),
    'disc1_is': (5, 'information-1', (127.9, 430.2, 139.9, 439.6)),
    'disc1_not': (5, 'is1', (166.3, 431.2, 175.4, 440.0)),
    'disc2_is': (5, 'information-2', (127.9, 411.2, 139.9, 420.6)),
    'disc2_not': (5, 'is-2', (166.3, 411.2, 175.4, 420.0)),
    'disc3_is': (5, 'information-3', (127.9, 393.2, 139.9, 402.6)),
    'disc3_not': (5, 'is-3', (166.3, 393.2, 175.4, 402.0)),
    'disc4_is': (5, 'information-4', (127.9, 374.2, 139.9, 383.6)),
    'disc4_not': (5, 'is-4', (166.7, 374.2, 175.4, 383.0)),
    'disc5_is': (5, 'information-5', (127.9, 356.0, 139.9, 365.3)),
    'disc5_not': (5, 'is-5', (166.3, 356.0, 175.4, 364.7)),
    'disc6_is': (5, 'information-6', (127.9, 337.0, 139.9, 346.3)),
    'disc6_not': (5, 'is-6', (166.3, 337.0, 175.4, 345.7)),
    'disc7_is': (5, 'information-7', (127.9, 319.0, 139.9, 328.3)),
    'disc7_not': (5, 'is-7', (166.3, 319.0, 175.4, 327.7)),
    'disc7_wholly': (5, 'information-71', (388.9, 318.0, 400.9, 327.3)),
    'disc7_partly': (5, 'is-72', (444.3, 318.0, 453.4, 326.7)),
    'disc8_is': (5, 'information-8', (127.9, 309.0, 139.9, 318.3)),
    'disc8_not': (5, 'is-8', (166.3, 309.0, 175.4, 317.7)),
    'disc_explain': (5, 'following specific repairs and treatments2', (79.1, 287.9, 577.0, 299.8)),
    'rsc_amount': (5, 'amount not exceeding', (371.5, 244.5, 466.7, 254.8)),
    'gov_programs_1': (5, 'or on the attached exhibit', (200.3, 198.4, 574.6, 209.5)),
    'gov_programs_2': (5, '2 on the attached', (79.6, 188.6, 575.5, 199.8)),
    'water_1': (5, 'temporary residential lease form promulgated by TREC or other written lease required by the 20', (65.1, 92.0, 77.1, 101.4)),
    'water_2': (5, 'temporary residential lease form promulgated by TREC or other written lease required by the 21', (65.1, 78.3, 77.1, 87.6)),
    'water_days': (5, 'Disclose if appl', (404.6, 77.4, 433.4, 86.8)),
    'water_3': (6, 'A Buyers Possession Seller shall deliver to Buyer possession of the Property in its present or required', (55.6, 717.6, 66.6, 726.4)),
    'water_provider_1': (6, 'Text30', (225.5, 611.5, 569.3, 622.9)),
    'water_provider_2': (6, 'Text31', (102.4, 601.0, 567.1, 612.1)),
    'p8_disclose_1': (6, 'percentage amount', (453.3, 540.7, 568.1, 552.4)),
    'p8_disclose_2': (6, 'dollar amount 2', (63.6, 530.4, 566.5, 541.6)),
    'closing_date': (6, 'A The closing of the sale will be on or before', (304.7, 509.5, 426.3, 523.5)),
    'closing_year': (6, '20', (449.9, 509.4, 478.0, 522.7)),
    'poss_closing': (6, 'dollar amount', (339.2, 312.1, 350.2, 321.0)),
    'poss_lease': (6, 'percentage', (485.4, 312.6, 497.4, 321.9)),
    'special_prov_1': (6, 'Text32', (350.5, 117.7, 566.2, 130.4)),
    'special_prov_2': (6, 'Text33', (64.3, 105.1, 566.2, 117.9)),
    'seller_concession': (6, 'b Seller shall also pay an amount not to exceed', (251.3, 41.2, 339.7, 52.4)),
    'b12_1_row': (7, 'temporary residential lease form promulgated by TREC or other written lease required by the', (64.2, 550.8, 76.2, 560.2)),
    'b12_1_dollar_box': (7, 'temporary residential lease form promulgated by TREC or other written lease required by the 23', (288.0, 551.1, 300.0, 560.5)),
    'b12_1_dollar': (7, 'Text34', (312.4, 549.3, 376.4, 560.4)),
    'b12_1_pct_box': (7, 'temporary residential lease form promulgated by TREC or other written lease required by the 24', (395.5, 551.3, 407.6, 560.6)),
    'b12_1_pct': (7, 'Text35', (409.0, 548.5, 449.4, 560.0)),
    'b12_2_row': (7, 'temporary residential lease form promulgated by TREC or other written lease required by the 25', (64.2, 531.1, 76.2, 540.4)),
    'b12_2_dollar_box': (7, 'temporary residential lease form promulgated by TREC or other written lease required by the 26', (287.9, 531.3, 300.0, 540.7)),
    'b12_2_dollar': (7, 'undefined_36', (311.8, 529.2, 374.3, 540.9)),
    'b12_2_pct_box': (7, 'temporary residential lease form promulgated by TREC or other written lease required by the 27', (395.5, 530.5, 407.5, 539.8)),
    'b12_2_pct': (7, 'undefined_37', (408.2, 529.3, 448.9, 540.6)),
    'n_buyer_addr': (8, 'at_2', (106.9, 220.3, 299.4, 231.9)),
    'n_buyer_phone': (8, 'Text2', (112.2, 184.5, 301.2, 199.2)),
    'n_buyer_email': (8, 'Text5', (108.6, 150.5, 299.4, 165.4)),
    'n_buyer_email_2': (8, 'Email', (62.0, 133.5, 299.0, 147.7)),
    'n_buyer_agent_addr': (8, 'Email-1', (106.0, 107.4, 302.9, 121.7)),
    'n_buyer_agent_email': (8, 'undefined_21', (93.2, 55.2, 299.9, 69.6)),
    'n_seller_addr': (8, 'at_3', (372.4, 219.4, 561.2, 230.7)),
    'n_seller_email': (8, 'Text9', (374.2, 150.5, 561.0, 167.0)),
    'n_seller_email_2': (8, 'Email_2', (326.9, 133.0, 561.0, 145.8)),
    'n_seller_agent_addr': (8, 'Email_2-2', (372.3, 107.9, 561.4, 121.2)),
    'n_seller_agent_email': (8, 'undefined_22', (360.2, 55.4, 560.5, 69.3)),
    'p22_tpf': (9, 'Environmental Assessment Threatened or', (61.4, 648.1, 72.7, 657.5)),
    'p22_sale_other': (9, 'Sellers Temporary Residential Lease', (61.4, 635.6, 72.9, 644.9)),
    'p22_appraisal': (9, 'Short Sale Addendum', (61.4, 622.6, 73.5, 632.0)),
    'p22_seller_fin': (9, 'Addendum for Property Located Seaward', (60.9, 609.5, 71.9, 619.4)),
    'p22_1031': (9, 'Addendum for Sellers Disclosure of', (60.4, 596.5, 71.9, 606.4)),
    'p22_short_sale': (9, 'Addendum for Property in a Propane Gas', (60.9, 583.6, 71.9, 593.5)),
    'p22_loan_assumption': (9, 'Other list', (61.9, 570.5, 72.3, 580.9)),
    'p22_release_va': (9, 'Fixture', (61.1, 556.4, 71.5, 566.8)),
    'p22_res_leases': (9, 'Section 1031', (61.8, 519.4, 71.5, 529.2)),
    'p22_fixture_leases': (9, 'Third Party Financing Addendum_2', (59.9, 506.7, 72.0, 516.6)),
    'p22_buyer_temp': (9, 'Seller Financing Addendum_2', (60.5, 493.3, 72.0, 503.2)),
    'p22_seller_temp': (9, 'Addendum for Property Subject to Mandatory', (60.5, 479.7, 72.5, 490.1)),
    'p22_hydrostatic': (9, 'Buyers Temporary Residential Lease', (61.0, 452.7, 72.0, 462.6)),
    'p22_environmental': (9, 'Loan Assumption Addendum', (60.5, 439.7, 72.0, 449.6)),
    'p22_lead_paint': (9, 'Addendum for Sale of Other Property by', (60.5, 408.7, 72.0, 419.1)),
    'p22_propane': (9, 'Addendum for BackUp Contract', (60.5, 389.7, 72.0, 399.6)),
    'p22_seaward': (9, 'Addendum for Coastal Area Property', (61.0, 374.7, 72.0, 384.6)),
    'p22_coastal': (9, 'Addendum for Authorizing Hydrostatic', (60.5, 362.7, 72.0, 372.6)),
    'p22_district_notices': (9, 'Addendum Concerning Right to Terminate', (60.5, 349.8, 72.0, 359.1)),
    'p22_district_list_1': (9, 'System Service Area', (256.0, 338.0, 567.4, 349.5)),
    'p22_poa': (9, 'Addendum for Reservation of Oil Gas and 20', (60.5, 277.8, 72.0, 287.2)),
    'p22_non_realty': (9, 'PID 10', (60.7, 265.1, 72.3, 274.4)),
    'p22_backup': (9, 'Addendum for Reservation of Oil Gas and', (61.1, 251.5, 72.6, 260.9)),
    'p22_mineral_reservation': (9, 'PID 11', (61.4, 237.8, 72.9, 247.1)),
    'p22_other': (9, 'PID 12', (61.2, 225.2, 72.8, 234.5)),
    'p22_other_text': (9, 'Attorney is_ 13', (117.3, 223.6, 567.6, 237.6)),
    'eff_day': (10, 'BROKER FILL IN THE DATE OF FINAL ACCEPTANCE', (146.3, 575.6, 192.2, 589.5)),
    'eff_month': (10, '20_2', (232.5, 575.6, 399.9, 588.5)),
    'eff_year': (10, 'Effective Date', (421.9, 575.6, 455.5, 589.0)),
    'ratification_fee': (11, 'Price when Listing Brokers fee is received Escrow Agent is authorized and directed to pay Other Broker from', (289.0, 715.9, 494.9, 729.9)),
    'fee_a_seller': (11, 'Seller_4', (100.8, 181.5, 112.4, 191.0)),
    'fee_a_buyer': (11, 'Buyer will pay ListingPrincipal Broker', (141.2, 181.5, 152.9, 191.0)),
    'fee_a_cash_box': (11, 'a cash fee of', (293.9, 181.9, 304.2, 191.0)),
    'fee_a_cash': (11, 'a cash fee of_2', (374.3, 180.8, 464.4, 191.9)),
    'fee_a_pct_box': (11, 'SellerBuyer authorizes and directs Escrow Agent to', (481.5, 182.5, 492.2, 190.8)),
    'fee_a_pct': (11, 'of the total Sales Price', (494.8, 180.1, 530.8, 191.8)),
    'fee_b_seller': (11, 'or', (172.2, 171.4, 184.4, 180.9)),
    'fee_b_buyer': (11, 'Seller_5', (215.4, 171.5, 226.2, 180.4)),
    'fee_b_cash_box': (11, 'Buyer will pay Other Broker', (386.0, 170.5, 397.5, 180.0)),
    'fee_b_cash': (11, 'of the total Sales Price and b', (479.2, 170.6, 555.4, 181.4)),
    'fee_b_pct_box': (11, 'agreement is attached a', (38.4, 161.5, 49.3, 171.0)),
    'fee_b_pct': (11, 'pay the brokers from the proceeds at closing', (52.2, 161.4, 87.2, 171.0)),
    'fee_sig_seller_1': (11, 'Signature20', (46.0, 90.3, 285.9, 110.3)),
    'fee_sig_buyer_1': (11, 'Signature23', (298.7, 90.3, 554.1, 110.1)),
    'rcpt_option': (12, 'is acknowledged', (107.0, 689.8, 214.8, 704.0)),
    'rcpt_em': (12, 'is acknowledged_2', (106.6, 614.0, 219.2, 626.8)),
    'rcpt_addl_em': (12, 'is acknowledged_3', (109.9, 367.6, 219.6, 380.8)),
}

# Page header "Contract Concerning ___ (Address of Property)", pages 2-12
# (spec 2b). Page 1 has none. 25-17 headers sit higher than the 20-19's.
HEADER_FIELDS = [(2, "Page 2 of 10"), (3, "Page 3 of 10"), (4, "Page 4 of 10"), (5, "Contract Concerning"),
                 (6, "Contract Concerning_2"), (7, "Contract Concerning_3"), (8, "Address of Property"),
                 (9, "Attorney is"), (10, "undefined_23"), (11, "Address of Property_24"),
                 (12, "Address of Property_2")]
HEADER_RECT = (120, 750, 446, 768)
# Initials footers are on pages 1-9 and are found by position (x0
# 211/253/347/396, bottom ~15-16pt), same as the 20-19: the /T names lie
# (page 8 "buyer 1, seller 1, seller 2, buyer 40" = Buyer1, Buyer2,
# Seller1, Seller2). tc_audit._initials_by_position's x-range and bottom fit.

# Paragraph 21 line-1 email fields only; line 2 can hold anything (spec 3).
NOTICE_EMAILS = [("Buyer's notice email (Paragraph 21)", "n_buyer_email"),
                 ("Seller's notice email (Paragraph 21)", "n_seller_email"),
                 ("Buyer's agent email (Paragraph 21 copy)", "n_buyer_agent_email"),
                 ("Seller's agent email (Paragraph 21 copy)", "n_seller_agent_email")]

# Spec 4.4: narrow on purpose. "oil", "gas", "water", "timber" alone are
# ordinary items ("propane gas tank", "water well", "timber fence").
_RESERVATION_RE = re.compile(r"\breserv(e|es|ed|ing|ation|ations)\b|\bmineral(s)?\b|\broyalt(y|ies)\b", re.I)

# Rural road names, longest first (txo-trec-qa, 2026-10-25). Applied after
# tc_audit._street, which already turns ROAD into RD and HIGHWAY into HWY.
_RURAL_ROADS = [("FARM TO MARKET RD", "FM"), ("FARM TO MARKET", "FM"), ("RANCH TO MARKET RD", "RM"),
                ("RANCH TO MARKET", "RM"), ("COUNTY RD", "CR"), ("CO RD", "CR"), ("RANCH RD", "RR"),
                ("PRIVATE RD", "PR"), ("STATE HWY", "SH")]
_LEGAL_DESC_RE = re.compile(r"\b(AC|ACS|ACRE|ACRES|ABSTRACT|ABS|SURVEY|TRACT|TR|LOT|BLK|BLOCK|EXHIBIT|SECTION)\b", re.I)


def _street_25_17(addr: str) -> str:
    """tc_audit._street, plus Texas rural road names folded to one form.
    Only used here, so the 20-19's header check is unchanged."""
    street = f" {A._street(addr)} "
    for long, short in _RURAL_ROADS:
        street = street.replace(f" {long} ", f" {short} ")
    return street.strip()


def header_issues(f, header_fields, header_rect, total, para) -> list:
    """The "Contract Concerning ___ (Address of Property)" header on each
    page vs the contract's address, only when that address is filled.
    Rural roads are written many ways ("County Road 210" / "CR 210"), and
    land headers often carry the legal description instead of the address
    (txo-trec-qa review, 2026-10-25). A different street number on a plain
    street address is still a must-fix; anything else is a review. Shared
    with trec_9_18 (para "Paragraph 2", 11 pages)."""
    issues = []
    street = _street_25_17(f.v("address"))
    if not street:
        return issues
    for page_no, name in header_fields:
        if page_no not in f.pages:
            continue
        header = f.values.get(name, "").strip() or A._text_in_rect(f.pages[page_no], header_rect)
        if not header:
            issues.append({"severity": "warning", "message": f"Page {page_no} of {total}: \"Address of Property\" header is blank",
                           "key": "header_address_blank"})
        elif _street_25_17(header) != street:
            if _LEGAL_DESC_RE.search(header) or not header[:1].isdigit():
                issues.append({"severity": "warning",
                               "message": f'Page {page_no} of {total}: the header reads "{header}" and {para}\'s address '
                                          f'is "{f.v("address")}". Confirm the header identifies this property.',
                               "key": "header_address_differs"})
            else:
                issues.append({"severity": "blocker",
                               "message": f'Page {page_no} of {total}: header address reads "{header}" but {para} says '
                                          f'"{f.v("address")}"',
                               "key": "header_address"})
    return issues


CORE_DRAFT = ["county", "escrow_agent", "earnest_money", "option_fee", "title_company", "description"]


def _name(role):
    return MAP[role][1]


def contract_pages(reader, form_id="25-17", total=12) -> dict:
    """{printed page number: page} for the 25-17 pages in a file, from each
    page's own footer, so an addendum merged in front never shifts them.
    trec_9_18 calls it with ("9-18", 11)."""
    base, ver = form_id.split("-")
    out = {}
    for page in reader.pages:
        text = page.extract_text() or ""
        if not re.search(rf"TREC\s*NO\.?\s*{base}\s*-\s*{ver}\b", text):
            continue
        m = re.search(rf"Page\s*(\d+)\s*of\s*{total}\b", text)
        if m and int(m.group(1)) not in out:
            out[int(m.group(1))] = page
    return out


class _Form:
    """A contract's values read by role. map_ is this module's MAP unless a
    sibling form module (trec_9_18) passes its own -- never another form's."""
    def __init__(self, values, pages, map_=None):
        self.values, self.pages, self.map = values, pages, map_ or MAP

    def v(self, role):
        return self.values.get(self.map[role][1], "").strip()

    def on(self, role):
        return _is_checked(self.values, self.map[role][1])

    def slot(self, role):
        """Field value, else text drawn inside the field's box."""
        page_no, name, rect = self.map[role]
        val = self.values.get(name, "").strip()
        if val or page_no not in self.pages:
            return val
        return A._text_in_rect(self.pages[page_no], rect)

    def money(self, role):
        return A._money(self.v(role))

    def effective_date(self):
        """Page 10 'EXECUTED the 20 day of October, 20 26' in the 25-17's own
        roles: day = 'BROKER FILL IN...', MONTH = '20_2', year = 'Effective Date'.
        (9-18 page 9: same roles, its own names -- see trec_9_18.MAP.)"""
        day = re.sub(r"\D", "", self.v("eff_day"))
        md = A._parse_month_day(f"{self.v('eff_month')} {day}")
        year = A._year(self.v("eff_year"))
        try:
            return date(year, *md) if md and year else None
        except ValueError:
            return None


def _issues(f: _Form) -> tuple:
    """(issues, core_missing) for the contract itself."""
    issues = []

    def add(sev, msg, key):
        issues.append({"severity": sev, "message": msg, "key": key})

    def one_of(label, roles, none_key=None):
        """A 'check one' group: more than one checked is a must-fix. With
        none_key, none checked is a review (spec 4.5); the groups where none
        is a must-fix (7B, 7K) say so themselves below."""
        checked = [lab for lab, role in roles if f.on(role)]
        if len(checked) > 1:
            add("blocker", f"{label}: {' and '.join(checked)} are both checked -- only one box is allowed", "check_one_conflict")
        elif not checked and none_key:
            add("warning", f"{label}: no box is checked", none_key)
        return checked

    core_missing = 0

    # --- Paragraph 2A: property description (spec 4.1) ----------------------
    described = any(f.v(r) for r in ("legal_desc_1", "legal_desc_2", "legal_desc_3", "address"))
    if not described:
        core_missing += 1
        add("warning", 'Paragraph 2A: no legal description or address is filled in. The form allows "as described '
                       'on attached exhibit"; confirm the exhibit is attached.', "property_description_blank")
    elif not (f.v("county") or f.v("county_2")):
        # Suppressed when the whole description is blank (exhibit).
        add("blocker", "Paragraph 2A: County is blank -- title will kick back the file without this", "county")
    if not (f.v("county") or f.v("county_2")):
        core_missing += 1
    # A blank address alone is no finding (rural land); there's no City blank.

    # --- Paragraph 1 parties (25-17 roles: Seller idx 0, Buyer idx 1) ------
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

    # --- Effective Date (page 10) ------------------------------------------
    if not (f.v("eff_day") and f.v("eff_month") and f.v("eff_year")):
        add("blocker", "Page 10 of 12: Effective Date is blank", "effective_date")

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

    # --- Paragraph 3D acreage adjustment (spec 4.2) ------------------------
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
        if f.on("survey_4"):
            add("warning", "Paragraph 3D adjusts the Sales Price \"based on the survey required by Paragraph 6C\", but "
                           "6C(4) says no survey is required", "acreage_adjustment_no_survey")
    elif will != ["will", "will not"] and acres_filled:
        add("warning", "Paragraph 3D says the Sales Price will not be adjusted (or neither box is checked), but the "
                       "acres or per-acre amount is filled in", "acreage_adjustment_conflict")

    # --- Paragraph 4 leases (spec 4.3, 25-17: "Check all applicable boxes")
    for box, p22, label in (("lease_4a_res", "p22_res_leases", "4A Residential Leases"),
                            ("lease_4b_fixture", "p22_fixture_leases", "4B Fixture Leases")):
        if f.on(box) and not f.on(p22):
            add("warning", f"Paragraph {label} is checked, and the form says that addendum is attached to this "
                           f"contract, but it isn't listed in Paragraph 22", "lease_addendum_not_listed")
    nrl = [r for r in ("nrl_1", "nrl_2") if f.on(r)]
    if f.on("lease_4c_nrl") and not nrl:
        add("warning", "Paragraph 4C (Natural Resource Leases) is checked, but neither 4C(1) nor 4C(2) is", "lease_incomplete")
    if len(nrl) == 2:
        add("warning", "Paragraph 4C: both 4C(1) (Seller has delivered) and 4C(2) (Seller has not delivered) are checked",
            "lease_conflict")
    if f.on("nrl_2") and not f.v("nrl_days"):
        add("warning", "Paragraph 4C(2): the number of days for Buyer to terminate is blank", "lease_incomplete")
    if nrl and not f.on("lease_4c_nrl"):
        add("warning", "Paragraph 4C(1)/(2) is checked, but the 4C Natural Resource Leases box isn't", "lease_incomplete")
    surface = [r for r in ("surface_1", "surface_2", "surface_3") if f.on(r)]
    if f.on("surface_2") and not (f.v("surface_2_text") or f.v("surface_2_text_2")):
        add("warning", "Paragraph 4D(2): oral Surface Leases are checked, but the lease isn't described (type of lease, "
                       "name of the tenant(s), rental amount, and term)", "lease_incomplete")
    if f.on("surface_3") and not f.v("surface_days"):
        add("warning", "Paragraph 4D(3): the number of days for Buyer to terminate is blank", "lease_incomplete")
    if surface and not f.on("lease_4d_surface"):
        add("warning", "Paragraph 4D(1)/(2)/(3) is checked, but the 4D Surface Leases box isn't", "lease_incomplete")

    # --- Paragraph 5B option days ------------------------------------------
    if f.v("option_fee") and not f.slot("option_days"):
        add("blocker", "Paragraph 5B: Option fee is entered but the option period (number of days) is blank", "option_days_blank")

    # --- Paragraph 6 title and survey (spec 4.5) ---------------------------
    one_of("Paragraph 6A (title policy expense)", [("Seller's", "title_exp_seller"), ("Buyer's", "title_exp_buyer")],
           none_key="title_expense_blank")
    short = one_of("Paragraph 6A(6) (shortages in area)", [("(i)", "shortages_i"), ("(ii)", "shortages_ii")])
    if short == ["(ii)"]:
        payer = [lab for lab, r in (("Buyer", "shortages_buyer"), ("Seller", "shortages_seller")) if f.on(r)]
        if len(payer) != 1:
            add("blocker", "Paragraph 6A(6)(ii): \"at the expense of [ ] Buyer [ ] Seller\" -- "
                           + ("both are checked" if payer else "neither is checked"), "check_one_conflict")
    survey = one_of("Paragraph 6C (Survey)", [("6C(1)", "survey_1"), ("6C(2)", "survey_2"), ("6C(3)", "survey_3"),
                                              ("6C(4)", "survey_4")], none_key="survey_box_missing")
    if len(survey) == 1 and survey[0] != "6C(4)":
        n = survey[0][3]
        if not f.v(f"survey_{n}_days"):
            add("blocker", f"Paragraph {survey[0]}: the number of days is blank", "survey_days_blank")
    if survey == ["6C(1)"]:
        one_of("Paragraph 6C(1) (new survey expense)", [("Seller's", "survey_1_new_seller"), ("Buyer's", "survey_1_new_buyer")],
               none_key="survey_expense_blank")
    if not f.v("objection_days"):
        add("warning", "Paragraph 6D: the number of days for Buyer to object is blank -- Buyer must object by the "
                       "earlier of the Closing Date or ___ days after receiving the title documents", "objection_days_blank")
    one_of("Paragraph 6G(7) (Texas Agricultural Development District)", [("is", "tadd_is"), ("is not", "tadd_is_not")])

    # --- Paragraph 7 property condition ------------------------------------
    if not one_of("Paragraph 7B (Seller's Disclosure Notice)", [("7B(1)", "sdn_1"), ("7B(2)", "sdn_2"), ("7B(3)", "sdn_3")]):
        add("blocker", "Paragraph 7B (Seller's Disclosure Notice): no box is checked -- one of 7B(1), 7B(2) or 7B(3) is required",
            "disclosure_box_missing")
    if f.on("sdn_2") and not f.v("sdn_days"):
        add("blocker", "Paragraph 7B(2): Seller's Disclosure delivery days are blank", "disclosure_days_blank")
    one_of("Paragraph 7D (Acceptance of condition)", [("7D(1)", "asis_1"), ("7D(2)", "asis_2")])
    aware = False
    for n in range(1, 9):
        if one_of(f"Paragraph 7H({n})", [("is", f"disc{n}_is"), ("is not", f"disc{n}_not")]) == ["is"]:
            aware = True
    part = one_of("Paragraph 7H(7)", [("wholly", "disc7_wholly"), ("partly", "disc7_partly")])
    if f.on("disc7_is") and not f.on("disc7_not") and not part:
        add("warning", "Paragraph 7H(7): \"is\" is checked, but neither \"wholly\" nor \"partly\" is", "disclosure_incomplete")
    if aware and not f.v("disc_explain"):
        add("warning", "Paragraph 7H: Seller is aware of an item above, but the explanation line is blank -- the form "
                       "says to explain (it may be on an attached sheet)", "disclosure_explain_blank")
    water = one_of("Paragraph 7K (Seller's Water Disclosure)", [("7K(1)", "water_1"), ("7K(2)", "water_2"), ("7K(3)", "water_3")])
    if not water:
        add("blocker", "Paragraph 7K (Seller's Water Disclosure): no box is checked -- the form says check one box only, "
                       "and 7K(3) covers a Seller who doesn't have to deliver it", "water_disclosure_box_missing")
    if f.on("water_2") and not f.v("water_days"):
        add("blocker", "Paragraph 7K(2): Seller's Water Disclosure delivery days are blank", "water_disclosure_days_blank")
    if f.on("water_3") and not (f.v("water_provider_1") or f.v("water_provider_2")):
        add("warning", "Paragraph 7K(3)(e): the water provider isn't named", "water_provider_blank")

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

    # --- 10A possession (spec 3 + 4.7) -------------------------------------
    one_of("Paragraph 10A (Possession)", [("upon closing and funding", "poss_closing"),
                                         ("temporary residential lease", "poss_lease")])
    if f.on("poss_lease") and not (f.on("p22_buyer_temp") or f.on("p22_seller_temp")):
        add("warning", "Paragraph 10A: possession is by a temporary residential lease, but neither TREC temporary lease "
                       "is listed in Paragraph 22 (another written lease is allowed -- confirm one is attached)",
            "possession_lease_not_listed")

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

    # --- Page 11 broker fee agreement (spec 4.8) ---------------------------
    fee_roles = ("fee_a_seller", "fee_a_buyer", "fee_a_cash_box", "fee_a_pct_box", "fee_b_seller", "fee_b_buyer",
                 "fee_b_cash_box", "fee_b_pct_box")
    fee_used = any(f.on(r) for r in fee_roles) or any(f.v(r) for r in ("fee_a_cash", "fee_a_pct", "fee_b_cash", "fee_b_pct"))
    if fee_used and (f.on("b12_1_row") or f.on("b12_2_row")):
        add("warning", "Page 11 broker fee agreement is filled in and Paragraph 12B is checked. The form says: \"DO NOT "
                       "SIGN IF THERE IS A SEPARATE AGREEMENT FOR PAYMENT OF BROKERS' FEES OR IF CONTRIBUTIONS ARE TO BE "
                       "PAID UNDER PARAGRAPH 12B(1) OR (2).\"", "fee_agreement_with_12b")
    for part_label, s, b, cb, ca, pb, pa in (("(a)", "fee_a_seller", "fee_a_buyer", "fee_a_cash_box", "fee_a_cash", "fee_a_pct_box", "fee_a_pct"),
                                             ("(b)", "fee_b_seller", "fee_b_buyer", "fee_b_cash_box", "fee_b_cash", "fee_b_pct_box", "fee_b_pct")):
        if f.on(s) and f.on(b):
            add("warning", f"Page 11 broker fee agreement {part_label}: both Seller and Buyer are checked", "fee_agreement_incomplete")
        if f.on(cb) and f.on(pb):
            add("warning", f"Page 11 broker fee agreement {part_label}: both the cash fee and the % box are checked", "fee_agreement_incomplete")
        if (f.on(cb) and not f.v(ca)) or (f.on(pb) and not f.v(pa)):
            add("warning", f"Page 11 broker fee agreement {part_label}: a box is checked but its amount is blank", "fee_agreement_incomplete")

    # --- Receipts on page 12 vs 5A -----------------------------------------
    for label, receipt, role, what in (("Option Fee receipt (page 12)", "rcpt_option", "option_fee", "option fee in Paragraph 5A"),
                                       ("Earnest Money receipt (page 12)", "rcpt_em", "earnest_money", "earnest money in Paragraph 5A")):
        got, want = f.money(receipt), f.money(role)
        if got is not None and want is not None and abs(got - want) > 0.005:
            add("blocker", f"{label} says {A._fmt_money(got)}, but the {what} is {A._fmt_money(want)}", "receipt_mismatch")

    # --- Address header, pages 2-12 (only when 2A's address is filled) -----
    issues.extend(header_issues(f, HEADER_FIELDS, HEADER_RECT, 12, "Paragraph 2A"))

    # --- Paragraph 11 ------------------------------------------------------
    provisions = " ".join(f.v(r) for r in ("special_prov_1", "special_prov_2")).strip()
    hit = A._BUSINESS_TERM_RE.search(provisions)
    if hit:
        snippet = provisions if len(provisions) <= 90 else provisions[:87] + "..."
        add("warning", f'Paragraph 11 (Special Provisions) reads like a business term ("{snippet}"). Paragraph 11 is for '
                       f"informational items only; licensees can't draft business terms there -- use a TREC addendum or "
                       f"have a party or attorney draft it", "special_provisions_business_term")

    # --- Reservations without an addendum (spec 4.4) -----------------------
    other_blank = not f.on("p22_other") and not f.v("p22_other_text")
    if not f.on("p22_mineral_reservation") and other_blank:
        for where, text in (("Paragraph 11", provisions),
                            ("Paragraph 2E", " ".join(f.v(r) for r in ("exclusions_1", "exclusions_2")).strip())):
            m = _RESERVATION_RE.search(text)
            if m:
                snippet = text if len(text) <= 90 else text[:87] + "..."
                add("warning", f"Paragraph 2F says any reservation of minerals, water, timber or other interests is made "
                               f'by an attached addendum. {where} mentions "{snippet}", but no reservation addendum is '
                               f"listed in Paragraph 22. If nothing is being reserved, this doesn't apply.", "reservation_without_addendum")
                break

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


# TREC 25-16 (11-04-2024) Effective Date, printed page 9 of 11. Rects from
# txo-trec-qa's review of bd80632 (2026-10-25); same role swap as 25-17:
# day, MONTH ("20_2"), year ("Effective Date"). Used only to word the
# outdated-form finding (signed before 25-17 was mandatory = info).
EFFECTIVE_25_16 = {
    "day": ("BROKER FILL IN THE DATE OF FINAL ACCEPTANCE", (146.7, 496.9, 192.7, 510.9)),
    "month": ("20_2", (233.0, 496.9, 400.4, 509.8)),
    "year": ("Effective Date", (422.3, 496.9, 455.9, 510.3)),
}


def effective_date_25_16(path):
    """date, or None when blank/unreadable. Read only from widgets on the
    page that says "TREC NO. 25-16" and "Page 9 of 11", by name AND rect
    (within 1pt), so a merged 25-17 or addendum page can't supply it."""
    for page in PdfReader(filecrypt.pdf_source(path)).pages:
        text = page.extract_text() or ""
        if not (re.search(r"TREC\s*NO\.?\s*25\s*-\s*16", text) and re.search(r"Page\s*9\s*of\s*11", text)):
            continue
        got = {}
        for name, value, _ft, rect in A._widget_rows(page):
            for part, (want, want_rect) in EFFECTIVE_25_16.items():
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


def _classify(pdf_paths, detections, all_values, main_index):
    """Extra files: by their printed TREC number first, then by the same
    field fingerprints check_tc_file uses (a 40-11 filled by another tool)."""
    fa = la = amend = None
    unknown = 0
    for i, (det, values) in enumerate(zip(detections, all_values)):
        if i == main_index:
            continue
        fid = det[0][0] if det else None
        if fid == "40-11" or (fid is None and A._matched_fa(values) >= A.MIN_MATCHED_FIELDS_FA
                              and A._matched_la(values) < A.MIN_MATCHED_LA):
            fa = values
        elif fid == "41-3" or (fid is None and A._matched_la(values) >= A.MIN_MATCHED_LA):
            la = values
        elif fid == "39-11" or (fid is None and A._matched_amend(values) >= A.MIN_MATCHED_AMEND):
            amend = values
        else:
            unknown += 1
    return fa, la, amend, unknown


def unreadable_result(form, page_count) -> dict:
    """Spec 1.5: the text says this form but the fields are gone (a flattened
    e-sign export or a scan with a text layer). Reading it would call every
    blank "missing"; it's the unreadable path, naming the form."""
    return {
        "recognized": False, "unreadable": True, "complete": False,
        "issues": [{"severity": "info", "key": "unrecognized",
                    "message": f"We recognized {form} but couldn't read its form fields. It may be scanned, "
                               "or flattened when it was signed or exported."}],
        "looks_like_blank_draft": False, "page_count": page_count, "has_addendum": False,
        "has_amendment": False, "has_loan_assumption": False,
        "severity": {"level": "unreadable", "emoji": "\u26AA", "label": "Couldn't read the form fields", "issue_count": 0},
    }


def attachment_issues(f, pdf_paths, detections, all_values, main_index, buyers, sellers) -> tuple:
    """(issues, has_addendum, has_loan_assumption, has_amendment): the 40-11,
    41-3 and 39-11 cross-checks plus outdated attachments. Read by role, so
    it works on any form whose map has tpf_3b_box, p22_tpf, financing_3b,
    la_3b_box, p22_loan_assumption, sf_3b_box, p22_release_va,
    sales_price_3c and address (25-17 and 9-18, spec 3)."""
    issues = []
    fa, la, amend, unknown = _classify(pdf_paths, detections, all_values, main_index)
    has_addendum, has_loan_assumption, has_amendment = fa is not None, la is not None, amend is not None

    def add(sev, msg, key):
        issues.append({"severity": sev, "message": msg, "key": key})

    if unknown:
        add("warning", "One of the uploaded files wasn't recognized as a TREC 40-11 Third Party Financing Addendum, 41-3 "
                       "Loan Assumption Addendum or 39-11 Amendment to Contract -- its consistency checks were skipped.",
            "extra_file_unrecognized")

    # 40-11 (spec 3: 25-17 6C(2) cites the Third Party Financing Addendum, so
    # 40-11 is the TPF here too). Same checks as the 20-19.
    loan = f.money("financing_3b")
    if has_addendum:
        label, b1, b2, s1, s2 = A.FA_INITIALS_PAGE
        issues.extend(A._check_initials_pair(label, [fa.get(b1, ""), fa.get(b2, "")], [fa.get(s1, ""), fa.get(s2, "")],
                                             buyers, sellers))
        if not has_loan_assumption:
            fa_loan = A._fa_total(fa)
            if loan is not None and fa_loan is not None and abs(loan - fa_loan) > 0.5:
                add("blocker", f"Paragraph 3B financing amount ({A._fmt_money(loan)}) doesn't match the 40-11 addendum's "
                               f"loan amount ({A._fmt_money(fa_loan)})", "loan_amount_mismatch")
        if not f.on("tpf_3b_box"):
            add("blocker", "Paragraph 3B: Third Party Financing Addendum checkbox not checked, but a 40-11 addendum is attached",
                "addendum_checkbox_mismatch")
        if not f.on("p22_tpf"):
            add("blocker", "Paragraph 22: Third Party Financing Addendum checkbox not checked, but a 40-11 addendum is attached",
                "addendum_checkbox_mismatch")
    else:
        b3, b22 = f.on("tpf_3b_box"), f.on("p22_tpf")
        msg = ("Third Party Financing is checked in Paragraph 3B and listed in Paragraph 22, but the 40-11 addendum isn't attached"
               if b3 and b22 else
               "Third Party Financing is checked in Paragraph 3B, but the 40-11 addendum is neither listed in Paragraph 22 nor attached"
               if b3 else
               "Paragraph 22 lists the Third Party Financing Addendum, but it isn't attached and Paragraph 3B financing isn't checked"
               if b22 else None)
        if msg:
            add("blocker", msg, "financing_addendum_missing")

    # 41-3: attached <-> 3B + 22, its own checks, and the 3B sum (skipped
    # when 3B Seller Financing is checked, as on the 20-19).
    la_3b, la_22 = f.on("la_3b_box"), f.on("p22_loan_assumption")
    if has_loan_assumption:
        label, b1, b2, s1, s2 = A.LA_INITIALS_PAGE
        issues.extend(A._check_initials_pair(label, [la.get(b1, ""), la.get(b2, "")], [la.get(s1, ""), la.get(s2, "")],
                                             buyers, sellers))
        if not la_3b:
            add("blocker", "Paragraph 3B: Loan Assumption Addendum checkbox not checked, but a 41-3 addendum is attached", "la_checkbox_mismatch")
        if not la_22:
            add("blocker", "Paragraph 22: Loan Assumption Addendum checkbox not checked, but a 41-3 addendum is attached", "la_checkbox_mismatch")
        # _loan_assumption_issues reads the contract by 20-19 names; hand it
        # the 25-17 values under those names (never the raw 25-17 dict: the
        # 20-19 Release-of-Liability box name is the 25-17 3B dollar field).
        shim = {FIELD_MAP_20_19["address"]: f.v("address"), A.RELEASE_22_BOX: f.values.get(f.map["p22_release_va"][1], "")}
        issues.extend(A._loan_assumption_issues(la, shim))
        assumed = [A._money(la.get(A.LA_FIELDS[f"note{n}_balance"], "")) for n in A._la_notes(la)]
        fa_loan = A._fa_total(fa) if has_addendum else 0
        tpf_unknown = not has_addendum and f.on("tpf_3b_box")
        if (loan is not None and assumed and None not in assumed and fa_loan is not None
                and not tpf_unknown and not f.on("sf_3b_box")):
            total = sum(assumed) + fa_loan
            if abs(total - loan) > 0.5:
                parts = " + ".join([f"40-11 loan {A._fmt_money(fa_loan)}"] * has_addendum + [f"assumed {A._fmt_money(a)}" for a in assumed])
                add("blocker", f"Paragraph 3B financing ({A._fmt_money(loan)}) doesn't match the attached financing: {parts} = "
                               f"{A._fmt_money(total)}", "loan_amount_mismatch")
    elif la_3b or la_22:
        where = " and ".join(p for p, on in (("checked in Paragraph 3B", la_3b), ("listed in Paragraph 22", la_22)) if on)
        add("blocker", f"Loan Assumption Addendum is {where}, but the 41-3 addendum isn't attached", "loan_assumption_addendum_missing")

    # 39-11 price and address (blank 39-11 address tolerated, spec 3).
    if has_amendment:
        old, new = f.money("sales_price_3c"), A._money(amend.get(AMEND_FIELDS["price_total"], "").strip())
        if _is_checked(amend, AMEND_FIELDS["price_checkbox"]) and old is not None and new is not None and abs(old - new) > 0.5:
            add("warning", f"39-11 box (1) changes the Sales Price from {A._fmt_money(old)} to {A._fmt_money(new)} -- "
                           f"confirm Paragraph 3B and the 40-11 loan amount reflect the new terms", A.AMEND_PRICE_CHANGE_KEY)
        amend_addr, main_addr = amend.get(AMEND_FIELDS["address"], "").strip(), f.v("address")
        if amend_addr and main_addr and A._normalized_address(amend_addr) != A._normalized_address(main_addr):
            add("warning", f"Amendment's property address ({amend_addr}) doesn't match the contract's ({main_addr}) -- "
                           f"confirm this amendment belongs to this file", A.AMEND_ADDRESS_MISMATCH_KEY)

    issues.extend(A._outdated_attachment_issues(detections))
    return issues, has_addendum, has_loan_assumption, has_amendment


def check_25_17(pdf_paths, detections, page_count) -> dict:
    """tc_audit.FORM_CHECKERS["25-17"]. Same result shape as check_tc_file's
    20-19 result, plus "form"."""
    main_index = next(i for i, det in enumerate(detections) if any(fid == "25-17" for fid, _ in det))
    main_path = pdf_paths[main_index]
    all_values = [_read_values(p) for p in pdf_paths]
    values = all_values[main_index]
    if sum(1 for _, name, _ in MAP.values() if name in values) < A.MIN_MATCHED_FIELDS:
        return unreadable_result(FORM, page_count)
    f = _Form(values, contract_pages(PdfReader(filecrypt.pdf_source(main_path))))

    issues, core_missing = _issues(f)

    buyers, sellers = A._party_names(f.v("buyer_name")), A._party_names(f.v("seller_name"))
    initials = []
    for page_label, buyer_boxes, seller_boxes in A._initials_by_position(f.pages):
        initials.extend(A._check_initials_pair(page_label, buyer_boxes, seller_boxes, buyers, sellers))
    issues.extend(initials)

    more, has_addendum, has_loan_assumption, has_amendment = attachment_issues(
        f, pdf_paths, detections, all_values, main_index, buyers, sellers)
    issues.extend(more)

    blocking = [i for i in issues if i["severity"] == "blocker"]
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
            # Land is often described, not addressed: show the first legal
            # description line when 2A's address is blank.
            "address": f.v("address") or f.v("legal_desc_1"),
            "city": "",
            "county": " ".join(x for x in (f.v("county"), f.v("county_2")) if x),
        },
    }
