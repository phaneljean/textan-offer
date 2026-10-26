"""
co_land.py -- TC File Check for the Colorado Real Estate Commission's CBS4
Contract to Buy and Sell Real Estate (Land), 2026 edition ("Adoption Date:
August 5, 2025 / Mandatory Use Date: January 1, 2026", no dated code),
downloaded from dre.colorado.gov, sha256 6022d5e42bfa38ad... Spec:
txo-trec-qa spec_co_land.md (2026-10-25).

Most CBS1 rules carry over (co_audit.py), renumbered: Closing is §3.1 item
43 on Land (39 on the CBS1), Possession 44-45, Acceptance 46. What's new is
§2.7 water and well rights, §8.9 mineral rights, the §9 survey and §10.6.4
environmental boxes, and the page-1 Residences / No Residences choice.

Fields are read by WIDGET (page + name + rect), never by name alone. Land
names are mostly honest, with traps (spec §1):
- §2.7.1 Deeded Water Rights' box is a second widget of "Solar Power Will
  Checkbox" (§2.5.5), and §2.7.4 Water Stock's shares a parent with the
  §15.7 water-transfer "Water Stock" box. The field VALUE turns "On" for
  both once either is checked, so each widget's own /AS is read. No /AS
  (a flattened third-party export) -> no finding on those boxes.
- "Buyer Date" is both the buyer's signature date and the buyer-side
  broker's date: no rule reads it.
- "Other Deed Checkbox" is a text field: §13 uses the deed description.
Rects are PyMuPDF's (origin top-left), from cbs4_widgets.json.
"""
import re

import fitz
import filecrypt
import co_audit
from co_audit import _parse_date, _money, _fmt_money, _blank_or_na, _issue, holiday_name, _PROPERTY_STATE_OK

FORM = "Colorado CBS4 (Land)"

MAP = {
    'no_res': (1, 'Property With No Residences Checkbox', (232.4, 211.6, 242.5, 221.6)),
    'res': (1, 'Property With Residences Checkbox', (155.6, 225.4, 165.6, 235.4)),
    'date': (1, 'Date', (434.5, 242.9, 572.5, 259.6)),
    'buyer': (1, 'Buyer Name', (138.0, 350.8, 487.0, 361.3)),
    'jt': (1, 'Joint Tenants Checkbox', (194.2, 363.2, 204.1, 373.3)),
    'tic': (1, 'Tenants In Common Checkbox', (271.8, 363.2, 281.8, 373.3)),
    'other_title': (1, 'Other Title Checkbox', (379.2, 363.2, 389.2, 373.3)),
    'seller': (1, 'Seller Name', (136.3, 390.4, 487.0, 401.9)),
    'county': (1, 'County', (443.9, 408.2, 527.4, 424.9)),
    'legal': (1, 'Property Legal Description', (49.9, 438.5, 572.3, 496.2)),
    'street': (1, 'Street Address', (95.4, 500.2, 320.4, 516.8)),
    'city': (1, 'City', (324.8, 500.2, 424.0, 516.8)),
    'state': (1, 'State', (428.4, 500.2, 518.5, 516.8)),
    'zip': (1, 'Zip', (523.0, 500.2, 572.4, 516.8)),
    'enc_will': (2, 'Encumbered Will Checkbox', (78.1, 58.4, 88.1, 68.4)),
    'enc_not': (2, 'Encumbered Will Not Checkbox', (113.2, 58.4, 123.1, 68.4)),
    'leased_will': (2, 'Leased Items Will Checkbox', (78.1, 184.9, 88.2, 194.9)),
    'leased_not': (2, 'Leased Items Will Not Checkbox', (113.3, 184.9, 123.4, 194.9)),
    'solar_will': (2, 'Solar Power Will Checkbox', (313.3, 242.4, 323.3, 252.4)),
    'solar_not': (2, 'Solar Power Will Not Checkbox', (347.5, 242.4, 357.5, 252.4)),
    'deeded_water': (2, 'Solar Power Will Checkbox', (51.2, 334.4, 61.1, 344.4)),
    'deeded_desc': (2, 'Deeded Water Rights Description', (49.9, 346.6, 576.2, 376.9)),
    'deeded_deed': (2, 'Deeded Water Rights Deed', (374.9, 379.3, 482.5, 390.0)),
    'other_water': (2, 'Other Rights Relating to Water Checkbox', (51.1, 391.9, 61.1, 401.9)),
    'other_water_desc': (2, 'Other Rights Relating to Water Description', (48.9, 413.7, 577.6, 469.7)),
    'well': (2, 'Well Rights Checkbox', (51.1, 472.4, 61.1, 482.3)),
    'well_permit': (2, 'Well Permit Number', (50.4, 540.2, 140.4, 550.9)),
    'water_stock': (2, 'Water Stock Checkbox', (51.1, 552.9, 61.1, 562.9)),
    'water_stock_desc': (2, 'Water Stock Description', (49.7, 564.2, 577.2, 598.9)),
    'taps_desc': (2, 'Water and Sewer Taps Description', (49.3, 621.8, 577.6, 654.7)),
    'ddl_will': (4, 'Deadlines Will Checkbox', (89.0, 363.4, 99.1, 373.4)),
    'ddl_not': (4, 'Deadlines Will Not Checkbox', (125.0, 363.4, 135.1, 373.4)),
    'price': (4, 'Purchase Price', (381.9, 439.4, 471.4, 450.5)),
    'earnest': (4, 'Earnest Money', (481.7, 451.4, 571.2, 462.5)),
    'new_loan': (4, 'New Loan', (481.7, 463.4, 571.2, 474.5)),
    'assumption': (4, 'Assumption Balance', (481.7, 475.4, 571.2, 486.5)),
    'private': (4, 'Private Financing', (481.7, 487.4, 571.2, 498.5)),
    'seller_fin': (4, 'Seller Financing', (482.1, 499.8, 571.7, 510.8)),
    'cash': (4, 'Case At Closing', (481.7, 535.4, 571.2, 546.5)),
    'total_left': (4, 'Total 01', (379.8, 547.4, 471.6, 558.6)),
    'total_right': (4, 'Total 02', (481.7, 547.4, 571.2, 558.6)),
    'earnest_holder': (4, 'Earnest Money Holder', (143.6, 645.2, 343.7, 655.9)),
    'funds_does': (5, 'Available Fund Does Checkbox', (461.9, 322.9, 471.8, 332.9)),
    'funds_not': (5, 'Available Fund Does Not Checkbox', (502.1, 322.9, 512.0, 332.9)),
    'loan_conv': (5, 'Loan Limitations Conventional Checkbox', (51.1, 426.4, 61.1, 436.3)),
    'loan_other': (5, 'Loan Limitations Other Checkbox', (127.3, 426.4, 137.3, 436.3)),
    'assump_will': (5, 'Assumption Seller Will Checkbox', (99.2, 541.3, 109.2, 551.4)),
    'assump_not': (5, 'Assumption Seller Will Not Checkbox', (136.0, 541.3, 145.9, 551.4)),
    'appr_buyer': (7, 'Cost of Appraisal Buyer Checkbox', (536.5, 219.4, 546.6, 229.4)),
    'appr_seller': (7, 'Cost of Appraisal Seller Checkbox', (51.1, 230.9, 61.1, 240.8)),
    'title_seller': (8, 'Seller Selects Title Checkbox', (51.2, 288.9, 61.1, 298.9)),
    'title_buyer': (8, 'Buyer Selects Title Checkbox', (51.1, 346.4, 61.1, 356.4)),
    'oec_will': (8, 'OEC Will Checkbox', (411.0, 392.4, 421.1, 402.4)),
    'oec_not': (8, 'OEC Will Not Checkbox', (452.3, 392.4, 462.2, 402.4)),
    'new_ilc': (10, 'New ILC Checkbox', (316.7, 461.4, 326.6, 471.4)),
    'new_survey': (10, 'New Survey Checkbox', (51.1, 472.8, 61.1, 482.9)),
    'survey_desc': (10, 'New Survey Description', (176.2, 471.8, 391.1, 482.4)),
    'order_seller': (10, 'Ordering New ILC Seller Checkbox', (309.8, 484.3, 319.9, 494.4)),
    'order_buyer': (10, 'Ordering New ILC Buyer Checkbox', (353.5, 484.3, 363.6, 494.4)),
    'pay_seller': (10, 'Payment of New ILC Seller Checkbox', (102.5, 530.4, 112.6, 540.4)),
    'pay_buyer': (10, 'Payment of New ILC Buyer Checkbox', (145.9, 530.4, 156.0, 540.4)),
    'pay_other': (10, 'Payment for New ILC Other Description', (49.7, 540.9, 577.8, 574.4)),
    'p1_order_seller': (13, 'Due Diligence Seller Checkbox', (266.9, 196.3, 277.0, 206.4)),
    'p1_order_buyer': (13, 'Due Diligence Buyer Checkbox', (311.3, 196.3, 321.2, 206.4)),
    'p1_exp_seller': (13, 'Due Diligence Expense Seller Checkbox', (363.6, 219.4, 373.6, 229.4)),
    'p1_exp_buyer': (13, 'Due Diligence Expense Buyer Checkbox', (407.0, 219.4, 417.0, 229.4)),
    'p2_seller': (13, 'Phase II Seller Checkbox', (338.4, 265.3, 348.4, 275.4)),
    'p2_buyer': (13, 'Phase II Buyer Checkbox', (385.0, 265.3, 395.0, 275.4)),
    'cond_addr': (13, 'Conditional Sale Address', (221.6, 413.8, 518.4, 424.4)),
    'potable_does': (13, 'Source of Potable Water Does Checkbox', (483.1, 472.3, 493.1, 482.4)),
    'potable_not': (13, 'Source of Potable Water Does Not Checkbox', (523.6, 472.3, 533.6, 482.4)),
    'no_well': (13, 'Source of Potable Water No Well Checkbox', (106.2, 495.4, 116.3, 505.3)),
    'permit_does': (13, 'Source of Potable Water Buyer Does Checkbox', (224.3, 495.4, 234.4, 505.3)),
    'permit_not': (13, 'Source of Potable Water Buyer Does Not Checkbox', (263.3, 495.4, 273.2, 505.3)),
    'ci_are': (14, 'Closing Instructions Are Checkbox', (430.6, 330.4, 440.5, 340.4)),
    'ci_not': (14, 'Closing Instructions Are Not Checkbox', (467.5, 330.4, 477.6, 340.4)),
    'deed_special': (14, 'Special Warranty Deed Checkbox', (565.2, 468.8, 575.2, 478.9)),
    'deed_general': (14, 'General Warranty Deed Checkbox', (141.7, 480.4, 151.8, 490.3)),
    'deed_bargain': (14, 'Bargain and Sale Deed Checkbox', (251.2, 480.4, 261.2, 490.3)),
    'deed_quit': (14, 'Quit Claim Deed Checkbox', (358.6, 480.4, 368.5, 490.3)),
    'deed_pr': (14, 'Personal Representative Deed Checkbox', (440.8, 480.4, 450.8, 490.3)),
    'deed_other_desc': (14, 'Other Deed Description', (65.5, 490.9, 205.4, 501.5)),
    'wt_stock': (15, 'Water Stock Checkbox', (231.1, 242.4, 241.1, 252.4)),
    'tax_cal': (15, 'Prorations Taxes for Calendar Checkbox', (190.0, 507.4, 200.0, 517.3)),
    'tax_mill': (15, 'Prorations Taxes Most Recent Mill Levy Checkbox', (466.0, 507.4, 476.0, 517.3)),
    'tax_other': (15, 'Prorations Taxes Other Checkbox', (349.6, 518.9, 359.6, 528.8)),
    'rents_actual': (15, 'Rents Actually Received Checkbox', (238.8, 530.4, 248.9, 540.4)),
    'rents_accrued': (15, 'Rents Accrued Checkbox', (362.0, 530.4, 372.1, 540.4)),
    'docs_part': (19, 'Documents Part of Contract', (49.1, 127.2, 572.7, 186.0)),
    'bwb_does': (20, 'Broker Working With Buyer Does Checkbox', (89.8, 59.4, 99.7, 69.4)),
    'bwb_not': (20, 'Broker Working With Buyer Does Not Checkbox', (130.1, 59.4, 140.0, 69.4)),
    'bwb_agent': (20, 'Broker Working With Buyer Buyers Agent Checkbox', (199.2, 128.9, 209.3, 139.0)),
    'bwb_tb': (20, 'Broker Working With Buyer Transaction Broker Checkbox', (282.1, 128.9, 292.2, 139.0)),
    'bwb_customer': (20, 'Broker Working With Buyer Customer Checkbox', (56.5, 152.4, 66.5, 162.5)),
    'bws_does': (20, 'Broker Working With Seller Does Checkbox', (89.8, 467.9, 99.7, 477.8)),
    'bws_not': (20, 'Broker Working With Seller Does Not Checkbox', (130.1, 467.9, 140.0, 477.8)),
    'dd_doc_1': (12, '10.6.1.6.1 Checkbox', (141.2, 265.4, 151.1, 275.5)),
    'dd_doc_2': (12, '10.6.1.6.2 Checkbox', (141.2, 288.4, 151.1, 298.5)),
    'dd_doc_3': (12, '10.6.1.6.3 Checkbox', (141.2, 299.9, 151.1, 310.0)),
    'dd_doc_4': (12, '10.6.1.6.4 Checkbox', (141.2, 334.4, 151.1, 344.5)),
    'dd_doc_5': (12, '10.6.1.6.5 Checkbox', (141.2, 345.9, 151.1, 356.0)),
    'dd_doc_6': (12, '10.6.1.6.6 Checkbox', (141.2, 357.4, 151.2, 367.5)),
    'dd_doc_7': (12, '10.6.1.6.7 Checkbox', (141.1, 368.8, 151.1, 378.9)),
    'dd_doc_8': (12, '10.6.1.6.8 Checkbox', (141.2, 391.9, 151.2, 401.9)),
    'dd_doc_9': (12, '10.6.1.6.9 Checkbox', (141.1, 414.9, 151.1, 425.0)),
    'dd_doc_10': (12, '10.6.1.6.10 Checkbox', (141.1, 437.9, 151.1, 447.9)),
    'dd_doc_11': (12, '10.6.1.6.11 Checkbox', (141.2, 495.3, 151.1, 505.4)),
    'dd_doc_12': (12, '10.6.1.6.12 Checkbox', (141.1, 518.4, 151.1, 528.5)),
    'dd_doc_13': (12, '10.6.1.6.13 Checkbox', (141.2, 552.9, 151.1, 563.0)),
    'item_1': (3, 'Time of Day Deadline', (399.7, 169.1, 579.4, 180.1)),
    'item_2': (3, 'Alternative Earnest Money Deadline', (399.7, 181.1, 579.4, 192.1)),
    'item_3': (3, 'Record Title Deadline and Tax Certificate', (399.7, 205.0, 579.4, 216.0)),
    'item_4': (3, 'Record Title Objection Deadline', (399.7, 217.0, 579.4, 228.0)),
    'item_5': (3, 'Off Record Title Deadline', (399.7, 229.0, 579.4, 240.0)),
    'item_6': (3, 'Off Record Title Objection Deadline', (399.7, 241.0, 579.4, 252.0)),
    'item_7': (3, 'Title Resolution Deadline', (399.7, 253.0, 579.4, 264.0)),
    'item_8': (3, 'Third Party Right to Purchase Approve Deadline', (399.7, 265.0, 579.4, 276.0)),
    'item_9': (3, 'Association Documents Deadline', (399.7, 289.0, 579.4, 300.0)),
    'item_10': (3, 'Association Documents Termination Deadline', (399.7, 301.0, 579.4, 312.0)),
    'item_11': (3, 'Sellers Property Disclosure Deadline', (399.7, 325.0, 579.4, 336.0)),
    'item_12': (3, 'Lead Based Paint Disclosure Deadline', (400.1, 337.3, 579.0, 359.5)),
    'item_13': (3, 'New Loan Application Deadline', (399.7, 372.5, 579.4, 383.5)),
    'item_14': (3, 'New Loan Terms Deadline', (399.7, 384.5, 579.4, 395.5)),
    'item_15': (3, 'New Loan Availability Deadline', (399.7, 396.5, 579.4, 407.5)),
    'item_16': (3, 'Buyers Credit Information Deadline', (399.7, 408.5, 579.4, 419.5)),
    'item_17': (3, 'Disapproval of Buyers Credit Information Deadline', (399.7, 420.5, 579.4, 431.5)),
    'item_18': (3, 'Existing Loan Deadline', (399.7, 432.5, 579.4, 443.5)),
    'item_19': (3, 'Existing Loan Termination Deadline', (399.7, 444.5, 579.4, 455.5)),
    'item_20': (3, 'Loan Transfer Approval Deadline', (399.7, 456.5, 579.4, 467.5)),
    'item_21': (3, 'Seller or Private Financing Deadline', (399.7, 468.5, 579.4, 479.5)),
    'item_22': (3, 'Appraisal Deadline', (399.7, 492.5, 579.4, 503.5)),
    'item_23': (3, 'Appraisal Objection Deadline', (399.7, 504.5, 579.4, 515.5)),
    'item_24': (3, 'Appraisal Resolution Deadline', (399.7, 516.5, 579.4, 527.5)),
    'item_25': (3, 'New ILC or New Survey Deadline', (399.7, 540.5, 579.4, 551.5)),
    'item_26': (3, 'New ILC or New Survey Objection Deadline', (399.7, 552.5, 579.4, 563.5)),
    'item_27': (3, 'New ILC or New Survey Resolution Deadline', (399.7, 564.5, 579.4, 575.5)),
    'item_28': (3, 'Water Rights Examination Deadline', (399.7, 588.5, 579.4, 599.5)),
    'item_29': (3, 'Mineral Rights Examination Deadline', (399.7, 600.5, 579.4, 611.5)),
    'item_30': (3, 'Inspection Termination Deadline', (399.7, 612.5, 579.4, 623.5)),
    'item_31': (3, 'Inspection Objection Deadline', (399.7, 624.5, 579.4, 635.5)),
    'item_32': (3, 'Inspection Resolution Deadline', (399.7, 636.5, 579.4, 647.5)),
    'item_33': (3, 'Property Insurance Termination Deadline', (399.7, 648.5, 579.4, 659.5)),
    'item_34': (3, 'Due Diligence Documents Delivery Deadline', (399.7, 660.5, 579.4, 671.5)),
    'item_35': (3, 'Due Diligence Documents Objection Deadline', (399.7, 672.5, 579.4, 683.5)),
    'item_36': (3, 'Due Diligence Documents Resolution Deadline', (399.7, 684.5, 579.4, 695.5)),
    'item_37': (3, 'Environmental Inspection Termination Deadline', (399.7, 696.5, 579.4, 707.5)),
    'item_38': (3, 'ADA Evaluation Termination Deadline', (399.7, 708.5, 579.4, 719.5)),
    'item_39': (3, 'Conditional Sale Deadline', (399.7, 720.5, 579.4, 731.5)),
    'item_40': (4, 'Lead Based Paint Termination Deadline', (400.1, 58.9, 579.0, 81.1)),
    'item_41': (4, 'Estoppel Statements Deadline', (399.7, 82.1, 579.4, 93.1)),
    'item_42': (4, 'Estoppel Statements Termination Deadline', (399.7, 94.1, 579.4, 105.1)),
    'item_43': (4, 'Closing Date', (399.7, 118.1, 579.4, 129.1)),
    'item_44': (4, 'Possession Date', (399.7, 130.1, 579.4, 141.1)),
    'item_45': (4, 'Possession Time', (399.7, 142.1, 579.4, 153.1)),
    'item_46': (4, 'Acceptance Deadline Date', (399.7, 154.1, 579.4, 165.1)),
    'item_47': (4, 'Acceptance Deadline Time', (399.7, 166.1, 579.4, 177.1)),
    'csf_buyer': (14, 'Closing Services Fee Buyer Checkbox', (488.0, 653.9, 498.0, 663.8)),
    'csf_seller': (14, 'Closing Services Fee Seller Checkbox', (537.0, 653.9, 547.0, 663.8)),
    'csf_half': (14, 'Closing Services Fee 50 50 Checkbox', (51.1, 665.4, 61.1, 675.4)),
    'csf_other': (14, 'Closing Services Fee Other Checkbox', (252.8, 665.4, 262.9, 675.4)),
    'rcf_buyer': (15, 'Record Change Fee Buyer Checkbox', (395.5, 58.4, 405.5, 68.4)),
    'rcf_seller': (15, 'Record Change Fee Seller Checkbox', (439.6, 58.4, 449.6, 68.4)),
    'rcf_half': (15, 'Record Change Fee 50 50 Checkbox', (482.0, 58.4, 492.0, 68.4)),
    'rcf_other': (15, 'Record Change Fee Not Applicable Checkbox', (154.7, 69.8, 164.8, 79.9)),
    'rwc_buyer': (15, 'Reserve or Working Capital Buyer Checkbox', (420.8, 92.9, 430.8, 103.0)),
    'rwc_seller': (15, 'Reserve or Working Capital Seller Checkbox', (466.1, 92.9, 476.0, 103.0)),
    'rwc_half': (15, 'Reserve or Working Capital 50 50 Checkbox', (509.6, 92.9, 519.6, 103.0)),
    'rwc_other': (15, 'Reserve or Working Capital Not Applicable Checkbox', (183.2, 104.4, 193.3, 114.4)),
    'ofee_buyer': (15, 'Other Fees Buyer Checkbox', (565.2, 115.9, 575.2, 125.9)),
    'ofee_seller': (15, 'Other Fees Seller Checkbox', (82.2, 127.4, 92.2, 137.4)),
    'ofee_half': (15, 'Other Fees 50 50 Checkbox', (125.6, 127.4, 135.7, 137.4)),
    'ofee_other': (15, 'Other Fees Not Applicable Checkbox', (327.4, 127.4, 337.4, 137.4)),
    'ltt_buyer': (15, 'Local Transfer Tax Buyer Checkbox', (414.6, 138.8, 424.6, 148.9)),
    'ltt_seller': (15, 'Local Transfer Tax Seller Checkbox', (462.1, 138.8, 472.2, 148.9)),
    'ltt_half': (15, 'Local Transfer Tax 50 50 Checkbox', (508.1, 138.8, 518.0, 148.9)),
    'ltt_other': (15, 'Local Transfer Tax Not Applicable Checkbox', (183.2, 150.4, 193.3, 160.4)),
    'sut_buyer': (15, 'Sales and Use Tax Buyer Checkbox', (51.1, 173.4, 61.1, 183.4)),
    'sut_seller': (15, 'Sales and Use Tax Seller Checkbox', (96.2, 173.4, 106.2, 183.4)),
    'sut_half': (15, 'Sales and Use Tax 50 50 Checkbox', (139.7, 173.4, 149.6, 183.4)),
    'sut_other': (15, 'Sales and Use Tax Not Applicable Checkbox', (341.4, 173.4, 351.5, 183.4)),
    'ptf_buyer': (15, 'Private Transfer Fee Buyer Checkbox', (485.9, 196.3, 496.0, 206.4)),
    'ptf_seller': (15, 'Private Transfer Fee Seller Checkbox', (536.5, 196.3, 546.5, 206.4)),
    'ptf_half': (15, 'Private Transfer Fee 50 50 Checkbox', (51.1, 207.8, 61.1, 217.9)),
    'ptf_other': (15, 'Private Transfer Fee Not Applicable Checkbox', (250.4, 207.8, 260.4, 217.9)),
    'wtf_buyer': (15, 'Water Transfer Buyer Checkbox', (177.5, 265.3, 187.4, 275.4)),
    'wtf_seller': (15, 'Water Transfer Seller Checkbox', (222.6, 265.3, 232.6, 275.4)),
    'wtf_half': (15, 'Water Transfer 50 50 Checkbox', (266.0, 265.3, 276.0, 275.4)),
    'wtf_other': (15, 'Water Transfer Not Applicable Checkbox', (465.4, 265.3, 475.3, 275.4)),
    'utf_buyer': (15, 'Utility Transfer Fees Buyer Checkbox', (85.8, 288.4, 95.9, 298.4)),
    'utf_seller': (15, 'Utility Transfer Fees Seller Checkbox', (130.9, 288.4, 140.9, 298.4)),
    'utf_half': (15, 'Utility Transfer Fees 50 50 Checkbox', (174.4, 288.4, 184.4, 298.4)),
    'utf_other': (15, 'Utility Transfer Fees Not Applicable Checkbox', (376.1, 288.4, 386.2, 298.4)),
    'c29_1': (18, '29.1 Checkbox', (73.6, 509.4, 83.6, 519.4)),
    'c29_1_pct': (18, '29.1 Percentage', (117.0, 508.3, 137.0, 519.0)),
    'c29_1_amt': (18, '29.1 Amount', (254.8, 508.3, 329.8, 519.0)),
    'c29_2': (18, '29.2 Checkbox', (73.6, 543.8, 83.6, 553.9)),
    'c29_2_pct': (18, '29.2 Percentage', (114.5, 542.8, 134.4, 553.4)),
    'c29_2_amt': (18, '29.2 Amount', (248.3, 542.8, 323.3, 553.4)),
    'c29_3': (18, '29.3 Checkbox', (73.6, 566.9, 83.6, 576.8)),
    'c29_3_pct': (18, '29.3 Percentage', (115.4, 565.8, 135.5, 576.5)),
    'c29_3_amt': (18, '29.3 Amount', (250.8, 565.8, 325.8, 576.5)),
}

# §3.1 rows by item number (Land): (key, label as printed). Items 1, 2, 12
# and 40 aren't compared (time of day, alternative earnest money, the two
# lead-paint rows -- the lead-paint rule reads them by role).
DEADLINES = {
    3: ("record_title", "Record Title Deadline"), 4: ("record_title_obj", "Record Title Objection Deadline"),
    5: ("off_record", "Off-Record Title Deadline"), 6: ("off_record_obj", "Off-Record Title Objection Deadline"),
    7: ("title_resolution", "Title Resolution Deadline"),
    8: ("third_party", "Third Party Right to Purchase/Approve Deadline"),
    9: ("assoc_docs", "Association Documents Deadline"), 10: ("assoc_term", "Association Documents Termination Deadline"),
    11: ("spd", "Seller's Property Disclosure Deadline"), 13: ("loan_app", "New Loan Application Deadline"),
    14: ("loan_terms", "New Loan Terms Deadline"), 15: ("loan_avail", "New Loan Availability Deadline"),
    16: ("credit", "Buyer's Credit Information Deadline"),
    17: ("credit_disapproval", "Disapproval of Buyer's Credit Information Deadline"),
    18: ("existing_loan", "Existing Loan Deadline"), 19: ("existing_loan_term", "Existing Loan Termination Deadline"),
    20: ("loan_transfer", "Loan Transfer Approval Deadline"), 21: ("seller_fin", "Seller or Private Financing Deadline"),
    22: ("appraisal", "Appraisal Deadline"), 23: ("appraisal_obj", "Appraisal Objection Deadline"),
    24: ("appraisal_res", "Appraisal Resolution Deadline"), 25: ("ilc", "New ILC or New Survey Deadline"),
    26: ("ilc_obj", "New ILC or New Survey Objection Deadline"), 27: ("ilc_res", "New ILC or New Survey Resolution Deadline"),
    28: ("water_exam", "Water Rights Examination Deadline"), 29: ("mineral_exam", "Mineral Rights Examination Deadline"),
    30: ("insp_term", "Inspection Termination Deadline"), 31: ("insp_obj", "Inspection Objection Deadline"),
    32: ("insp_res", "Inspection Resolution Deadline"), 33: ("insurance_term", "Property Insurance Termination Deadline"),
    34: ("dd_delivery", "Due Diligence Documents Delivery Deadline"),
    35: ("dd_obj", "Due Diligence Documents Objection Deadline"),
    36: ("dd_res", "Due Diligence Documents Resolution Deadline"),
    37: ("env_term", "Environmental Inspection Termination Deadline"),
    38: ("ada_term", "ADA Evaluation Termination Deadline"), 39: ("conditional_sale", "Conditional Sale Deadline"),
    41: ("estoppel", "Estoppel Statements Deadline"), 42: ("estoppel_term", "Estoppel Statements Termination Deadline"),
    43: ("closing", "Closing Date"), 44: ("possession", "Possession Date"), 46: ("acceptance", "Acceptance Deadline Date"),
}
ITEM = {key: item for item, (key, _) in DEADLINES.items()}
SEQUENCES = [chain for chain in co_audit.SEQUENCES] + [["estoppel", "estoppel_term"]]
NOT_BEFORE_CLOSING = co_audit.NOT_BEFORE_CLOSING

# Two-way "check one" choices (spec §2), Land widgets.
EXCLUSIVE_PAIRS = [
    ("§2.5.2 encumbered inclusions", "enc_will", "enc_not"),
    ("§2.5.4 leased items", "leased_will", "leased_not"),
    ("§2.5.5 solar power plan", "solar_will", "solar_not"),
    ("§3.3.3 holiday extension", "ddl_will", "ddl_not"),
    ("§4.4.3 Available Funds", "funds_does", "funds_not"),
    ("§4.6 release of liability", "assump_will", "assump_not"),
    ("§8.1 title company selection", "title_seller", "title_buyer"),
    ("§8.1.3 OEC", "oec_will", "oec_not"),
    ("§6 cost of appraisal", "appr_buyer", "appr_seller"),
    ("§10.8 source of potable water", "potable_does", "potable_not"),
    ("§12.2 closing instructions", "ci_are", "ci_not"),
    ("Buyer's broker earnest money receipt", "bwb_does", "bwb_not"),
    ("Seller's broker earnest money receipt", "bws_does", "bws_not"),
    # Land-only check-one groups (spec §3)
    ("Page 1 Property with No Residences / with Residences", "no_res", "res"),
    ("§9.1 New ILC / New Survey", "new_ilc", "new_survey"),
    ("§9.1.1 who orders the New ILC or New Survey", "order_seller", "order_buyer"),
    ("§9.1.2 who pays for the New ILC or New Survey", "pay_seller", "pay_buyer"),
    ("§10.6.4 who orders the Environmental Inspection", "p1_order_seller", "p1_order_buyer"),
    ("§10.6.4 Environmental Inspection expense", "p1_exp_seller", "p1_exp_buyer"),
    ("§10.6.4 Phase II cost", "p2_seller", "p2_buyer"),
    ("§10.8 well permit acknowledgment", "permit_does", "permit_not"),
    ("§16.1.2 rents", "rents_actual", "rents_accrued"),
]
MULTI_GROUPS = [
    ("§2.1 how Buyer takes title", ["jt", "tic", "other_title"]),
    ("§16.1.1 taxes", ["tax_cal", "tax_mill", "tax_other"]),
]
DEED_BOXES = ["deed_special", "deed_general", "deed_bargain", "deed_quit", "deed_pr"]
PAYER_GROUPS = [
    ("§15.2 Closing Services Fee", "csf"), ("§15.3.2 Record Change Fee", "rcf"),
    ("§15.3.3 Reserves or Working Capital", "rwc"), ("§15.3.4 Other Fees", "ofee"),
    ("§15.4 Local Transfer Tax", "ltt"), ("§15.5 Sales and Use Tax", "sut"), ("§15.6 Private Transfer Fee", "ptf"),
    ("§15.7 Water Transfer Fees", "wtf"), ("§15.8 Utility Transfer Fees", "utf"),
]
# Read per widget (/AS), never by field value: shared parents (module doc).
SHARED = {"solar_will", "deeded_water", "water_stock", "wt_stock"}
# Fewer roles than this on their own pages = fields gone (flattened export).
MIN_ROLES = 20
CORE_DRAFT = ["county", "price", "earnest", "earnest_holder", "item_43"]


class LandForm:
    """The CBS4's widgets, by role. text(role) -> str; on(role) -> True /
    False, or None when the box has no readable /AS of its own."""
    def __init__(self, pdf_path):
        doc = fitz.open(stream=_read_bytes(pdf_path), filetype="pdf")
        self.texts, self.boxes, self.found = {}, {}, set()
        by_page = {}
        for page in doc:
            for w in page.widgets():
                by_page.setdefault(page.number + 1, []).append(
                    (w.field_name, tuple(w.rect), w.field_type, w.field_value, _own_state(doc, w)))
        for role, (page_no, name, rect) in MAP.items():
            hit = next((w for w in by_page.get(page_no, []) if w[0] == name and _near(w[1], rect)), None)
            if hit is None:
                continue
            self.found.add(role)
            _, _, ftype, value, state = hit
            if ftype == fitz.PDF_WIDGET_TYPE_CHECKBOX:
                self.boxes[role] = state if (state is not None or role in SHARED) else _value_on(value)
            else:
                self.texts[role] = (value or "").strip() if isinstance(value, str) else ""
        doc.close()

    def text(self, role):
        return self.texts.get(role, "")

    def on(self, role):
        return self.boxes.get(role, False)

    def item(self, n):
        return self.text(f"item_{n}")

    def readable(self):
        return len(self.found) >= MIN_ROLES


def _read_bytes(pdf_path):
    src = filecrypt.pdf_source(pdf_path)
    if isinstance(src, str):
        with open(src, "rb") as f:
            return f.read()
    return src.getvalue()


def _near(a, b, tol=3.0):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def _value_on(value) -> bool:
    return bool(value) and str(value).lstrip("/") not in ("Off", "", "No", "False")


def _own_state(doc, w):
    """This widget's own appearance state: True/False, None if absent."""
    if w.field_type != fitz.PDF_WIDGET_TYPE_CHECKBOX:
        return None
    kind, val = doc.xref_get_key(w.xref, "AS")
    if kind != "name":
        return None
    return val.lstrip("/") != "Off"


def audit(f: LandForm) -> list:
    issues = []

    def add(sev, msg, key):
        issues.append(_issue(sev, msg, key))

    # --- Property and parties (§2) -------------------------------------
    # Rural parcels often have no street address or city; §2.4's operative
    # text is the legal description (spec §2).
    legal, street = f.text("legal"), f.text("street")
    if not legal and not street:
        add("blocker", "§2.4: Legal description and street address are both blank -- nothing identifies the Property",
            "co_land_property_blank")
    elif not legal:
        add("warning", "§2.4: Legal description is blank -- title will ask for it before closing", "co_legal_blank")
    if not f.text("county"):
        add("blocker", "§2.4: County is blank -- the title commitment and deed need it", "co_county_blank")
    if not f.text("buyer"):
        add("warning", "§2.1: Buyer name is blank", "co_buyer_blank")
    if not f.text("seller"):
        add("warning", "§2.3: Seller name is blank", "co_seller_blank")
    state = f.text("state").upper().replace(".", "")
    if state and state not in _PROPERTY_STATE_OK:
        add("blocker", f"§2.4: Property state is \"{f.text('state')}\" -- the CBS4 is only for Colorado property",
            "co_state_not_colorado")

    # --- Price and terms (§4.1) -----------------------------------------
    # Land's Cash at Closing field is named "Case At Closing" (sic) and its
    # private-financing field "Private Financing"; MAP reads them by rect.
    price = _money(f.text("price"))
    if price is None:
        add("blocker", "§4.1: Purchase Price is blank", "co_price_blank")
    if _money(f.text("earnest")) is None:
        add("blocker", "§4.1 / §4.3: Earnest Money amount is blank", "co_earnest_blank")
    if not f.text("earnest_holder"):
        add("blocker", "§4.3: Earnest Money Holder is blank -- no one is named to hold the deposit", "co_earnest_holder_blank")
    right_col = {k: _money(f.text(k)) for k in ("earnest", "new_loan", "assumption", "private", "seller_fin", "cash")}
    filled = [x for x in right_col.values() if x is not None]
    total_right, total_left = _money(f.text("total_right")), _money(f.text("total_left"))
    if price is not None and filled:
        s = sum(filled)
        if abs(s - price) > 0.5:
            add("blocker", f"§4.1: Earnest Money + loan + Cash at Closing add up to {_fmt_money(s)}, not the "
                           f"{_fmt_money(price)} Purchase Price", "co_price_math")
        elif total_right is not None and abs(total_right - s) > 0.5:
            add("warning", f"§4.1: TOTAL (right column) says {_fmt_money(total_right)}, but the items above it add up "
                           f"to {_fmt_money(s)}", "co_total_right")
    if price is not None and total_left is not None and abs(total_left - price) > 0.5:
        add("warning", f"§4.1: TOTAL (left column) says {_fmt_money(total_left)}, but the Purchase Price is "
                       f"{_fmt_money(price)}", "co_total_left")

    # --- Financing (§4.5, §5) --------------------------------------------
    new_loan = right_col["new_loan"] or 0
    loan_types = [label for label, role in (("Conventional", "loan_conv"), ("Other", "loan_other")) if f.on(role)]
    # Review, not must-fix: Land §4.5.3 says Buyer "may purchase" with any
    # of Conventional / Other, so neither checked isn't an impossible state.
    if new_loan > 0 and not loan_types:
        add("warning", "§4.5.3: a New Loan amount is listed, but neither Conventional nor Other is checked",
            "co_land_loan_type_unchecked")
    if loan_types and new_loan == 0:
        add("warning", f"§4.5.3: {'/'.join(loan_types)} is checked, but §4.1 shows no New Loan amount", "co_loan_amount_missing")
    if new_loan > 0:
        for item, key, consequence in [
            (15, "loan_avail", "Buyer's §5.2.2 right to terminate if the loan isn't approved is deleted"),
            (14, "loan_terms", "Buyer's §5.2.1 right to terminate over loan terms is deleted"),
        ]:
            raw = f.item(item)
            if _blank_or_na(raw):
                add("warning", f"§3.1 item {item}: {DEADLINES[item][1]} is {'blank' if not raw else raw} with a New Loan in "
                               f"§4.1 -- under §3.2 that means {consequence}. Confirm that's intended.", f"co_{key}_deleted")
        if _blank_or_na(f.item(23)):
            add("warning", "§3.1 item 23: Appraisal Objection Deadline is blank -- under §3.2 the buyer has no appraisal "
                           "objection right. Confirm that's intended.", "co_appraisal_deleted")
    if (right_col["seller_fin"] or right_col["private"] or 0) > 0 and _blank_or_na(f.item(21)):
        add("warning", "§3.1 item 21: Seller or Private Financing Deadline is blank, but §4.1 lists seller/private financing",
            "co_seller_fin_deadline_blank")
    if (right_col["assumption"] or 0) > 0 and _blank_or_na(f.item(20)):
        add("warning", "§3.1 item 20: Loan Transfer Approval Deadline is blank, but §4.1 lists an Assumption Balance",
            "co_assumption_deadline_blank")
    if not (f.on("funds_does") or f.on("funds_not")):
        add("warning", "§4.4.3: neither Does nor Does Not is checked for Available Funds", "co_available_funds_unchecked")

    # --- Contingencies deleted by a blank deadline (§3.2) ----------------
    if _blank_or_na(f.item(31)) and _blank_or_na(f.item(30)):
        add("warning", "§3.1 items 30-31: Inspection Objection and Inspection Termination Deadlines are both blank -- "
                       "under §3.2 the buyer has no inspection right. Confirm that's intended.", "co_inspection_deleted")
    if _blank_or_na(f.item(4)):
        add("warning", "§3.1 item 4: Record Title Objection Deadline is blank -- under §3.2 the buyer can't object to the "
                       "title commitment", "co_title_objection_deleted")

    # --- Closing (item 43), possession (44-45), acceptance (46) ----------
    closing_raw = f.item(43)
    closing = _parse_date(closing_raw)
    if _blank_or_na(closing_raw):
        add("blocker", "§3.1 item 43: Closing Date is blank", "co_closing_blank")
    elif closing and closing.weekday() >= 5:
        add("warning", f"§3.1 item 43: Closing Date ({closing_raw}) is a {closing.strftime('%A')} -- confirm the closing "
                       f"company is open", "co_closing_weekend")
    elif closing and holiday_name(closing):
        add("warning", f"§3.1 item 43: Closing Date ({closing_raw}) is {holiday_name(closing)} -- banks may not move "
                       "wire funds; confirm the closing company and lender can fund", "co_closing_holiday")
    if _blank_or_na(f.item(44)) or _blank_or_na(f.item(45)):
        add("warning", "§3.1 items 44-45: Possession Date or Possession Time is blank", "co_possession_blank")
    acc_raw = f.item(46)
    acc, poss = _parse_date(acc_raw), _parse_date(f.item(44))
    if _blank_or_na(acc_raw):
        add("warning", "§3.1 item 46: Acceptance Deadline Date is blank -- the offer has no stated expiration",
            "co_acceptance_blank")
    if closing and poss and poss < closing:
        add("warning", f"Possession Date ({poss:%m/%d/%Y}) is before the Closing Date ({closing:%m/%d/%Y}) -- "
                       "early possession usually needs a separate agreement", "co_possession_before_closing")
    if closing and acc and acc > closing:
        add("blocker", f"Acceptance Deadline ({acc:%m/%d/%Y}) is after the Closing Date ({closing:%m/%d/%Y})",
            "co_acceptance_after_closing")

    # --- Deadline order ----------------------------------------------------
    dates = {key: (_parse_date(f.item(item)), item, label) for item, (key, label) in DEADLINES.items()}
    for chain in SEQUENCES:
        dated = [(k, *dates[k]) for k in chain if dates[k][0]]
        for (k1, d1, i1, l1), (k2, d2, i2, l2) in zip(dated, dated[1:]):
            if d2 < d1:
                add("blocker", f"§3.1: {l2} (item {i2}, {d2:%m/%d/%Y}) is before {l1} (item {i1}, {d1:%m/%d/%Y})",
                    f"co_order_{k1}_{k2}")
    if closing:
        for key, (d, item, label) in dates.items():
            if key not in NOT_BEFORE_CLOSING and d and d > closing:
                add("blocker", f"§3.1 item {item}: {label} ({d:%m/%d/%Y}) is after the Closing Date ({closing:%m/%d/%Y})",
                    f"co_after_closing_{key}")

    # --- Check-one conflicts ---------------------------------------------
    for label, a, b in EXCLUSIVE_PAIRS:
        if f.on(a) and f.on(b):
            add("blocker", f"{label}: both boxes are checked", f"co_conflict_{a}")
    for label, roles in MULTI_GROUPS:
        if sum(1 for r in roles if f.on(r)) > 1:
            add("blocker", f"{label}: more than one box is checked", f"co_conflict_{roles[0]}")
    deeds = sum(1 for r in DEED_BOXES if f.on(r)) + (1 if f.text("deed_other_desc") else 0)
    if deeds > 1:
        add("blocker", "§13 deed: more than one kind of deed is selected", "co_conflict_deed")
    for label, row in PAYER_GROUPS:
        if sum(1 for who in ("buyer", "seller", "half", "other") if f.on(f"{row}_{who}")) > 1:
            add("blocker", f"{label}: more than one payer is checked", f"co_conflict_payer_{row}")
    if not (f.on("title_seller") or f.on("title_buyer")):
        add("warning", "§8.1: neither Seller Selects nor Buyer Selects Title Insurance Company is checked",
            "co_title_selection_unchecked")

    # --- §29 buyer's brokerage compensation -------------------------------
    for n in ("1", "2", "3"):
        if f.on(f"c29_{n}") and not (f.text(f"c29_{n}_pct") or f.text(f"c29_{n}_amt")):
            add("warning", f"§29: compensation line {n} is checked, but both the % and $ amount are blank", f"co_comp_{n}_blank")

    # --- Page 1 and lead paint --------------------------------------------
    # §10.10 is "[Intentionally Deleted - See Residential Addendum if
    # applicable]" on Land: the lead-paint rows only matter with Residences.
    no_res, res = f.on("no_res"), f.on("res")
    if not no_res and not res:
        add("warning", "Page 1: neither 'Property with No Residences' nor 'Property with Residences-Residential Addendum "
                       "Attached' is checked.", "co_land_residence_unchecked")
    if res and not no_res and not f.item(12) and not f.item(40):
        add("warning", "§3.1 items 12 and 40: Lead-Based Paint deadlines are blank -- required if a home on the Property "
                       "was built before 1978, otherwise write N/A", "co_lead_paint_blank")
    if not f.text("date"):
        add("warning", "Page 1: contract Date is blank", "co_date_blank")

    # --- §2.7 water and §8.9 mineral rights (blank only: "N/A" = decided) --
    water_boxes = [f.on(r) for r in ("deeded_water", "other_water", "well", "water_stock")]
    water_text = any(f.text(r) for r in ("deeded_desc", "other_water_desc", "water_stock_desc", "taps_desc"))
    if (any(water_boxes) or water_text) and not f.item(28):
        add("warning", "§3.1 item 28 Water Rights Examination Deadline is blank. Under §3.2 that deletes §2.7.7 (Buyer's "
                       "Right to Terminate if examination of the Water Rights is unsatisfactory).", "co_land_water_review_deleted")
    if not f.item(29):
        add("warning", "§3.1 item 29 Mineral Rights Examination Deadline is blank. Under §3.2 that deletes §8.9 Mineral "
                       "Rights Review.", "co_land_mineral_review_deleted")
    if f.on("deeded_water"):
        if not f.text("deeded_desc"):
            add("warning", "§2.7.1 is checked but 'The following legally described water rights:' is blank",
                "co_land_deeded_water_blank")
        if not f.text("deeded_deed"):
            add("warning", "§2.7.1 is checked but the deed type for the water rights is blank", "co_land_water_deed_blank")
    if f.on("other_water") and not f.text("other_water_desc"):
        add("warning", "§2.7.2 is checked but the Other Rights Relating to Water description is blank",
            "co_land_other_water_blank")
    if f.on("water_stock") and not f.text("water_stock_desc"):
        add("warning", "§2.7.4 is checked but the water stock description is blank", "co_land_water_stock_blank")
    if f.on("well") and not f.text("well_permit"):
        add("warning", "§2.7.3 Well Rights is checked but the Well Permit # is blank -- confirm whether the well is "
                       "registered", "co_land_well_permit_blank")
    if f.on("well") and f.on("no_well"):
        add("warning", "§2.7.3 Well Rights is checked, but §10.8 says 'There is No Well'", "co_land_well_conflict")
    if f.on("no_well") and f.on("permit_does"):
        add("warning", "§10.8 says 'There is No Well', but also that Buyer Does acknowledge receipt of the well permit",
            "co_land_no_well_permit")
    # "(Residential Land and Residential Improvements Only)": silent on vacant land.
    if res and not (f.on("potable_does") or f.on("potable_not")):
        add("warning", "§10.8: neither Does nor Does Not is checked for the Seller's Property Disclosure on the source of "
                       "potable water", "co_land_potable_unchecked")

    # --- §9 survey ----------------------------------------------------------
    survey_box = f.on("new_ilc") or f.on("new_survey")
    survey_items = [f.item(n) for n in (25, 26, 27)]
    if f.on("new_survey") and not f.text("survey_desc"):
        add("warning", "§9.1 'New Survey in the form of ___' is blank.", "co_land_survey_form_blank")
    if survey_box and not any(survey_items):
        add("warning", "§9.1: a New ILC or New Survey is selected, but §3.1 items 25-27 are all blank -- under §3.2 the "
                       "survey deadlines are deleted", "co_land_survey_deadlines_blank")
    if survey_box and not (f.on("order_seller") or f.on("order_buyer")):
        add("warning", "§9.1.1: a New ILC or New Survey is selected, but neither Seller nor Buyer is checked to order it",
            "co_land_survey_order_unchecked")
    if survey_box and not (f.on("pay_seller") or f.on("pay_buyer") or f.text("pay_other")):
        add("warning", "§9.1.2: a New ILC or New Survey is selected, but no one is named to pay for it",
            "co_land_survey_payer_unchecked")
    if not survey_box and any(_parse_date(x) for x in survey_items):
        add("warning", "§9.1: §3.1 items 25-27 are dated but no New ILC/New Survey is selected",
            "co_land_survey_unselected")

    # --- §10.6 due diligence, §10.7 conditional sale, §11 estoppel ---------
    if any(f.on(f"dd_doc_{n}") for n in range(1, 14)) and not f.item(34):
        add("warning", "§10.6.1.6: Due Diligence Documents are checked, but §3.1 item 34 is blank -- under §3.2 that "
                       "deletes Due Diligence Documents delivery", "co_land_dd_delivery_blank")
    if (f.on("p1_order_seller") or f.on("p1_order_buyer")) and _blank_or_na(f.item(37)):
        add("warning", "§10.6.4: an Environmental Inspection is to be ordered, but §3.1 item 37 Environmental Inspection "
                       "Termination Deadline is blank", "co_land_env_deadline_blank")
    if f.text("cond_addr") and _blank_or_na(f.item(39)):
        add("warning", "§10.7: a Conditional Sale property is listed, but §3.1 item 39 Conditional Sale Deadline is blank",
            "co_land_conditional_sale_deadline_blank")
    if _parse_date(f.item(39)) and not f.text("cond_addr"):
        add("warning", "§3.1 item 39 Conditional Sale Deadline is dated, but §10.7 names no property to be sold",
            "co_land_conditional_sale_address_blank")
    if bool(_parse_date(f.item(41))) != bool(_parse_date(f.item(42))):
        add("warning", "§11: only one of §3.1 items 41 (Estoppel Statements Deadline) and 42 (Estoppel Statements "
                       "Termination Deadline) is dated", "co_land_estoppel_incomplete")
    return issues


# --- SPD19L Seller's Property Disclosure (Land): recognized + 2 reviews ----
_SPD19L = re.compile(r"SPD19LSELLER.SPROPERTYDISCLOSURE\(LAND\)")


def is_spd19l(pdf_path) -> bool:
    from pypdf import PdfReader
    try:
        text = PdfReader(filecrypt.pdf_source(pdf_path)).pages[0].extract_text() or ""
    except Exception:
        return False
    return bool(_SPD19L.search(re.sub(r"\s+", "", text)))


def _spd_issues(f: LandForm, spd: dict) -> list:
    """No checks on the disclosure answers: they're the seller's statements."""
    issues = []
    prop = co_audit._v(spd, "Property")
    street, county = f.text("street"), f.text("county")
    want = street or county
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    if prop and want and norm(want) not in norm(prop):
        issues.append(_issue("warning", f"Seller's Property Disclosure (Land): Property ({prop}) doesn't contain the "
                                        f"contract's {'street' if street else 'county'} ({want}) -- confirm it belongs "
                                        f"to this file", "co_spd_address_mismatch"))
    permit, c_permit = co_audit._v(spd, "B1 Well Permit Number"), f.text("well_permit")
    if permit and c_permit and norm(permit) != norm(c_permit):
        issues.append(_issue("warning", f"Seller's Property Disclosure (Land) well permit ({permit}) differs from the "
                                        f"contract's §2.7.3 Well Permit # ({c_permit})", "co_spd_well_permit_mismatch"))
    return issues


AMEND_NOT_CHECKED = ("{label}: not cross-checked against a Land contract yet (its §3.1 rows are mapped to the "
                     "Residential contract), so nothing in it was checked")


def check_land(pdf_paths, all_values, contract_index, page_count, summary) -> dict:
    """Same result shape as co_audit.check_co_file."""
    import co_forms
    import trec_25_17
    f = LandForm(pdf_paths[contract_index])
    if not f.readable():
        result = trec_25_17.unreadable_result(FORM, page_count)
        result["state"] = "CO"
        return result
    issues = audit(f)
    values = all_values[contract_index]
    spd_idx = [n for n, p in enumerate(pdf_paths) if n != contract_index and is_spd19l(p)]
    others, new_2027, edition_issues, unknown = co_audit.companions(pdf_paths, all_values, values, skip=spd_idx)
    # AE41/CP40 cells and the NTC43 rule are keyed by CBS1 rows (spec §2).
    later = [(k, v) for k, v in others if k in ("ae41", "ctr", "insp")]
    checked = [(k, v) for k, v in others if k not in ("ae41", "ctr", "insp")]
    # co_forms.cross_check reads the CBS1's "Documents Part of the Contract";
    # Land's §31.1 field is "Documents Part of Contract".
    contract = dict(values)
    contract["Documents Part of the Contract"] = f.text("docs_part")
    issues += co_forms.cross_check(contract, checked, also_attached=new_2027 + [k for k, _ in later])
    issues += edition_issues
    issues += [co_forms.not_checked_2027(k) for k in new_2027]
    for k, _ in later:
        issues.append(_issue("info", AMEND_NOT_CHECKED.format(label=co_forms.FORM_NAMES[k]), f"co_land_{k}_not_checked"))
    if f.on("res") and not f.on("no_res") and "resadd" not in {k for k, _ in others} | set(new_2027):
        issues.append(_issue("warning", "Page 1 says Residential Addendum Attached; no Residential Addendum is in this "
                                        "upload.", "co_land_resadd_missing"))
    for n in spd_idx:
        issues += _spd_issues(f, all_values[n])
    if unknown:
        issues.append(_issue("warning",
                             f"{unknown} attached file{'s' if unknown != 1 else ''} wasn't recognized as a Colorado form "
                             f"this check reads -- not checked", "co_other_files_not_checked"))
    blockers = [i for i in issues if i["severity"] == "blocker"]
    core_missing = sum(1 for r in CORE_DRAFT if not f.text(r))
    return {
        "recognized": True,
        "form": FORM,
        "state": "CO",
        "complete": not blockers,
        "issues": issues,
        "looks_like_blank_draft": core_missing / len(CORE_DRAFT) >= 0.7,
        "page_count": page_count,
        "has_addendum": False,
        "has_amendment": any(k == "ae41" for k, _ in others),
        "has_loan_assumption": False,
        "co_attachments": [co_forms.FORM_NAMES[k] for k, _ in others]
                          + [f"{co_forms.FORM_NAMES[k]} (2027 edition)" for k in new_2027]
                          + ["Seller's Property Disclosure (Land)"] * len(spd_idx),
        "severity": summary(issues) if summary else None,
        "property": {"address": f.text("street"), "city": f.text("city"), "county": f.text("county")},
    }
