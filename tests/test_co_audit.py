"""Colorado CBS1 TC Check rules (co_audit.py), via tc_audit.check_tc_file's
dispatch. Every file is built from the blank 2026 CBS1 by tests/co_fixtures.py:
CLEAN must come back with no issues, and each single-error variant must
raise exactly the rule it plants.

    python3 -m unittest tests.test_co_audit
"""
import os
import tempfile
import unittest

from tc_audit import check_tc_file
from tests.co_fixtures import fill, BLANK

HERE = os.path.dirname(os.path.abspath(__file__))
TREC_CLEAN = os.path.join(HERE, "fixtures", "trec_20-19_clean_two_parties.pdf")


class CoAuditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_variant(self, overrides=None):
        path = os.path.join(self.tmp.name, f"v{abs(hash(repr(overrides)))}.pdf")
        fill(overrides, out=path)
        return check_tc_file([path])

    def keys(self, overrides):
        return [i["key"] for i in self.run_variant(overrides)["issues"]]

    def assertOnly(self, overrides, *expected):
        self.assertEqual(sorted(self.keys(overrides)), sorted(expected))

    # --- recognition -------------------------------------------------------
    def test_clean_deal_is_clean(self):
        r = self.run_variant()
        self.assertTrue(r["recognized"])
        self.assertEqual(r["form"], "Colorado CBS1")
        self.assertEqual(r["issues"], [])
        self.assertEqual(r["severity"]["level"], "clear")
        self.assertEqual(r["property"], {"address": "123 Main St", "city": "Denver", "county": "Denver"})

    def test_blank_form_is_recognized_as_blank_draft(self):
        r = check_tc_file([BLANK])
        self.assertTrue(r["recognized"])
        self.assertTrue(r["looks_like_blank_draft"])
        self.assertFalse(r["complete"])

    def test_trec_still_routes_to_trec(self):
        r = check_tc_file([TREC_CLEAN])
        self.assertTrue(r["recognized"])
        self.assertNotEqual(r.get("form"), "Colorado CBS1")

    # --- required fields ---------------------------------------------------
    def test_blank_county(self):
        self.assertOnly({"County": ""}, "co_county_blank")

    def test_blank_earnest_holder(self):
        self.assertOnly({"Earnest Money Holder": ""}, "co_earnest_holder_blank")

    def test_blank_closing_date(self):
        self.assertOnly({"Closing Date": ""}, "co_closing_blank")

    def test_non_colorado_state(self):
        self.assertOnly({"State": "TX"}, "co_state_not_colorado")

    # --- §4.1 price math ---------------------------------------------------
    def test_price_math(self):
        self.assertOnly({"Cash At Closing": "80,000.00"}, "co_price_math")

    def test_total_right_typo(self):
        self.assertOnly({"Total 02": "510,000.00"}, "co_total_right")

    def test_total_left_typo(self):
        self.assertOnly({"Total 01": "490,000.00"}, "co_total_left")

    # --- financing ---------------------------------------------------------
    def test_loan_without_type(self):
        self.assertOnly({"Conventional Checkbox": False}, "co_loan_type_missing")

    def test_loan_availability_deleted(self):
        self.assertOnly({"New Loan Availability Deadline": "N/A"}, "co_loan_avail_deleted")

    def test_cash_deal_without_loan_deadlines_is_clean(self):
        self.assertOnly({"New Loan": None, "Cash At Closing": "490,000.00", "Conventional Checkbox": False,
                         "New Loan Application Deadline": None, "New Loan Terms Deadline": None,
                         "New Loan Availability Deadline": None, "Appraisal Deadline": None,
                         "Appraisal Objection Deadline": None, "Appraisal Resolution Deadline": None})

    def test_appraisal_deleted_not_flagged_for_fha(self):
        self.assertOnly({"Appraisal Objection Deadline": "", "Conventional Checkbox": False, "FHA Checkbox": True})

    def test_available_funds_unchecked(self):
        self.assertOnly({"Available Funds Does Checkbox": False}, "co_available_funds_unchecked")

    # --- §3.2 deleted contingencies -----------------------------------------
    def test_inspection_deleted(self):
        self.assertOnly({"Inspection Objection Deadline": "", "Inspection Termination Deadline": "Deleted",
                         "Inspection Resolution Deadline": ""}, "co_inspection_deleted")

    def test_title_objection_deleted(self):
        self.assertOnly({"Record Title Objection Deadline": ""}, "co_title_objection_deleted")

    # --- dates -------------------------------------------------------------
    def test_objection_before_delivery(self):
        self.assertOnly({"Record Title Objection Deadline": "10/06/2026"}, "co_order_record_title_record_title_obj")

    def test_deadline_after_closing(self):
        self.assertOnly({"Inspection Resolution Deadline": "11/02/2026"}, "co_after_closing_insp_res")

    def test_weekend_closing(self):
        self.assertOnly({"Closing Date": "10/31/2026", "Possession Date": "10/31/2026"}, "co_closing_weekend")

    def test_holiday_closing(self):
        self.assertOnly({"Closing Date": "11/11/2026", "Possession Date": "11/11/2026"}, "co_closing_holiday")

    def test_holidays(self):
        from co_audit import holiday_name
        from datetime import date
        self.assertEqual(holiday_name(date(2026, 11, 26)), "Thanksgiving")
        self.assertEqual(holiday_name(date(2026, 10, 5)), "Cabrini Day (Colorado)")
        self.assertEqual(holiday_name(date(2026, 7, 3)), "Independence Day (observed)")  # July 4 2026 is a Saturday
        self.assertIsNone(holiday_name(date(2026, 10, 30)))

    def test_possession_before_closing(self):
        self.assertOnly({"Possession Date": "10/28/2026"}, "co_possession_before_closing")

    def test_acceptance_after_closing(self):
        self.assertOnly({"Acceptance Deadline Date": "11/05/2026"}, "co_acceptance_after_closing")

    def test_relative_and_written_dates(self):
        # "MEC + 7" can't be ordered and isn't guessed at; "October 15, 2026" parses.
        self.assertOnly({"Record Title Deadline and Tax Certificate": "MEC + 7",
                         "Record Title Objection Deadline": "October 15, 2026"})

    # --- conflicts ---------------------------------------------------------
    def test_both_title_boxes(self):
        self.assertOnly({"Buyer Selects Title Checkbox": True}, "co_conflict_Seller Selects Title Checkbox")

    def test_no_title_box(self):
        self.assertOnly({"Seller Selects Title Checkbox": False}, "co_title_selection_unchecked")

    def test_two_payers(self):
        self.assertOnly({"Closing Services Fee Buyer Checkbox": True}, "co_conflict_Closing Services Fee Buyer Checkbox")

    def test_lead_paint_blank(self):
        self.assertOnly({"Lead Based Paint Disclosure Deadline": None, "LeadBased Paint Termination Deadline": None},
                        "co_lead_paint_blank")

    def test_comp_line_without_amount(self):
        self.assertOnly({"29.1 Checkbox": True}, "co_comp_1_blank")

    def test_comp_line_with_amount_is_clean(self):
        self.assertOnly({"29.1 Checkbox": True, "29.1 Percentage": "2.5"})


if __name__ == "__main__":
    unittest.main()
