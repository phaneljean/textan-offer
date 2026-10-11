"""TC Check rules, against two fixture files:

- trec_20-19_planted_errors.pdf: a reviewer's test contract with 16 planted
  errors (1 buyer, 1 seller). Every one must be reported.
- trec_20-19_clean_two_parties.pdf + trec_40-11_clean.pdf: the same deal
  with everything correct and 2 buyers + 2 sellers. Must come back clean.
  Single-error variants of it prove each rule fires on its own.

    python3 -m unittest tests.test_tc_audit
"""
import os
import tempfile
import unittest

import fitz  # PyMuPDF, only used here to make single-error variants

from tc_audit import check_tc_file

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
PLANTED = os.path.join(FIX, "trec_20-19_planted_errors.pdf")
CLEAN = os.path.join(FIX, "trec_20-19_clean_two_parties.pdf")
CLEAN_FA = os.path.join(FIX, "trec_40-11_clean.pdf")


def keys(result):
    return [i["key"] for i in result["issues"]]


def variant(text=None, checks=None, initials=None):
    """Copy of the clean contract with some fields changed. initials maps
    a 0-based page index to the 4 footer box values."""
    d = fitz.open(CLEAN)
    for pi, page in enumerate(d):
        foot = sorted([w for w in page.widgets() if w.field_type_string == "Text"
                       and 200 <= w.rect.x0 <= 440 and w.rect.y1 > 750], key=lambda w: w.rect.x0)
        if initials and pi in initials:
            for w, v in zip(foot, initials[pi]):
                w.field_value = v or " "   # PyMuPDF ignores "", and the audit strips spaces
                w.update()
        for w in page.widgets():
            if w in foot:
                continue
            if text and w.field_name in text:
                w.field_value = text[w.field_name] or " "
                w.update()
            elif checks and w.field_name in checks:
                w.field_value = w.on_state() if checks[w.field_name] else "Off"
                w.update()
    path = os.path.join(tempfile.mkdtemp(), "variant.pdf")
    d.save(path)
    return check_tc_file([path, CLEAN_FA])


class PlantedErrors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = check_tc_file([PLANTED])
        cls.msgs = [i["message"] for i in cls.r["issues"]]

    def has(self, key, *fragments):
        hits = [i["message"] for i in self.r["issues"] if i["key"] == key and all(f in i["message"] for f in fragments)]
        self.assertTrue(hits, f"no {key} issue containing {fragments}; got {self.msgs}")

    def test_initials_one_per_party(self):
        self.has("initials_buyer", "Page 4 of 12")
        self.has("initials_seller", "Page 6 of 12")
        self.has("initials_buyer", "Page 7 of 12")      # page 7's boxes are named "AC numb 1-4"
        flagged = {i["message"].split(":")[0] for i in self.r["issues"] if i["key"].startswith("initials_")}
        for page in ("Page 1 of 12", "Page 2 of 12", "Page 3 of 12", "Page 5 of 12", "Page 9 of 12"):
            self.assertNotIn(page, flagged, "complete page flagged for initials")

    def test_wrong_person_initials_is_blocker(self):
        self.has("initials_mismatch", "Page 8 of 12", '"JM"')
        self.assertEqual({i["severity"] for i in self.r["issues"] if i["key"] == "initials_mismatch"}, {"blocker"})

    def test_rules(self):
        self.has("sales_price_math", "$425,000", "$435,000")
        self.has("closing_date_invalid", "November 31")
        self.has("option_days_blank")
        self.has("check_one_conflict", "6C(1)", "6C(3)")
        self.has("disclosure_days_blank", "7B(2)")
        self.has("broker_contribution_incomplete", "12B(1)")
        self.has("receipt_mismatch", "$200", "$250")
        self.has("header_address", "Page 9 of 12", "1243 Elm")
        self.has("header_address_blank", "Page 5 of 12")
        self.has("email_invalid", "john.doe@gmailcom")
        self.has("poa_addendum_missing")
        self.has("financing_addendum_missing", "neither listed in Paragraph 22 nor attached")
        self.has("special_provisions_business_term", "$5,000")


class DemoSample(unittest.TestCase):
    def test_demo_sample_shows_the_range(self):
        # static/sample_trec_20-19.pdf is the "Run a sample check" file on the
        # homepage and /tc-check: the planted-errors contract at 123 Main St.
        r = check_tc_file([os.path.join(HERE, "..", "static", "sample_trec_20-19.pdf")])
        self.assertGreaterEqual(len(set(keys(r))), 14)


class CleanControl(unittest.TestCase):
    def test_clean_file_has_no_issues(self):
        r = check_tc_file([CLEAN, CLEAN_FA])
        self.assertEqual(r["issues"], [])
        self.assertEqual(r["severity"]["level"], "clear")

    def test_second_buyer_missing_on_one_page(self):
        r = variant(initials={3: ["JD", "", "JS", "MS"]})
        self.assertEqual(keys(r), ["initials_buyer"])
        self.assertIn("Page 4 of 12", r["issues"][0]["message"])
        self.assertIn("1 of 2", r["issues"][0]["message"])

    def test_second_seller_initials_wrong(self):
        r = variant(initials={6: ["JD", "MD", "JS", "KL"]})
        self.assertEqual(keys(r), ["initials_mismatch"])

    # Paragraph 1 wording Texas files really use. Each one used to raise
    # 10-20 false "initials_mismatch" blockers on a correct file.
    BUYER = "Seller and"
    SELLER = "1 PARTIES The parties to this contract are"

    def test_and_wife_wording_is_clean(self):
        self.assertEqual(keys(variant(text={self.BUYER: "John Doe and wife, Mary Doe"})), [])

    def test_married_couple_wording_is_clean(self):
        self.assertEqual(keys(variant(text={self.BUYER: "John Doe and Mary Doe, a married couple"})), [])

    def test_single_woman_wording_is_clean(self):
        # One seller, so seller2 boxes are blank on the contract. The 40-11
        # fixture still carries seller2 "MS", which is a real mismatch
        # against a lone "Jane Smith" -- so only the contract's own pages
        # are asserted here.
        r = variant(text={self.SELLER: "Jane Smith, a single woman"},
                    initials={i: ["JD", "MD", "JS", ""] for i in range(9)})
        contract = [i["key"] for i in r["issues"] if not i["message"].startswith("40-11 addendum")]
        self.assertEqual(contract, [])

    def test_hyphenated_surname_is_clean(self):
        r = variant(text={self.BUYER: "John Doe and Mary Smith-Doe"},
                    initials={0: ["JD", "MSD", "JS", "MS"]})
        self.assertEqual(keys(r), [])

    def test_last_first_order_is_clean(self):
        self.assertEqual(keys(variant(text={self.SELLER: "Smith, Jane and Smith, Mark"})), [])

    def test_wrong_initials_still_fire_with_and_wife(self):
        r = variant(text={self.BUYER: "John Doe and wife, Mary Doe"},
                    initials={6: ["JD", "KL", "JS", "MS"]})
        self.assertEqual(keys(r), ["initials_mismatch"])

    def test_closing_before_effective(self):
        self.assertEqual(keys(variant(text={"A The closing of the sale will be on or before": "October 1"})),
                         ["closing_before_effective"])

    def test_no_7b_box(self):
        r = variant(checks={"2 Buyer accepts the Property As Is provided Seller at Sellers expense shall complete the": False})
        self.assertEqual(keys(r), ["disclosure_box_missing"])

    def test_cash_portion_blank(self):
        r = variant(text={"undefined_3": ""})
        self.assertIn("cash_portion_blank", keys(r))

    def test_informational_provision_not_flagged(self):
        self.assertEqual(keys(variant()), [])


CLEAN_LA = os.path.join(FIX, "trec_41-3_clean.pdf")


def _edit(src, text=None, checks=None):
    d = fitz.open(src)
    for page in d:
        for w in page.widgets():
            if text and w.field_name in text:
                w.field_value = text[w.field_name] or " "
                w.update()
            elif checks and w.field_name in checks:
                w.field_value = w.on_state() if checks[w.field_name] else "Off"
                w.update()
    path = os.path.join(tempfile.mkdtemp(), os.path.basename(src))
    d.save(path)
    return path


# The clean deal, switched to an assumption: 3B/22 Third Party Financing off,
# Loan Assumption + Release of Liability on, 3B = the assumed balance.
ASSUME_ONLY = dict(
    text={"undefined_3": "145,000.00", "undefined_4": "280,000.00"},
    checks={"B Sum of all financing described in the attached": False, "Addendum for Reservation of Oil Gas": False,
            "Check Box2": True, "Environmental Assessment Threatened or": True, "Third Party Financing Addendum": True})


def assumption(contract=None, la=None, with_contract=True):
    c = _edit(CLEAN, **{**ASSUME_ONLY, **(contract or {})}) if with_contract else None
    paths = [p for p in (c, _edit(CLEAN_LA, **(la or {}))) if p]
    return check_tc_file(paths)


class LoanAssumption(unittest.TestCase):
    def test_clean_assumption_is_clean(self):
        r = assumption()
        self.assertTrue(r["has_loan_assumption"])
        self.assertEqual(keys(r), [])

    def test_with_40_11_sums_both_into_3b(self):
        c = _edit(CLEAN, text={"undefined_3": "5,000.00", "undefined_4": "620,000.00", "undefined_5": "625,000.00"},
                  checks={"Check Box2": True, "Environmental Assessment Threatened or": True, "Third Party Financing Addendum": True})
        r = check_tc_file([c, CLEAN_FA, _edit(CLEAN_LA)])
        self.assertEqual(keys(r), [])
        c = _edit(CLEAN, checks={"Check Box2": True, "Environmental Assessment Threatened or": True, "Third Party Financing Addendum": True})
        r = check_tc_file([c, CLEAN_FA, _edit(CLEAN_LA)])
        self.assertEqual(keys(r), ["loan_amount_mismatch"])

    def test_listed_but_not_attached(self):
        c = _edit(CLEAN, **ASSUME_ONLY)
        r = check_tc_file([c])
        self.assertEqual(keys(r), ["loan_assumption_addendum_missing"])

    def test_attached_but_not_listed(self):
        r = assumption(contract={"checks": {**ASSUME_ONLY["checks"], "Check Box2": False}})
        self.assertEqual(keys(r), ["la_checkbox_mismatch"])

    def test_each_blank_fires_alone(self):
        cases = {"within": "la_credit_days_blank", "undefined_2": "la_note_incomplete",
                 "which unpaid balance at closing will be": "la_note_incomplete",
                 "2 an increase in the interest rate to more than": "la_rate_cap_blank",
                 "1 payment of an assumption fee in excess of": "la_fee_cap_blank",
                 "undefinedNo 5": "la_variance_cap_blank"}
        for field, key in cases.items():
            with self.subTest(field=field):
                self.assertEqual(keys(assumption(la={"text": {field: ""}})), [key])

    def test_no_note_checked(self):
        r = assumption(la={"checks": {"1 The unpaid principal balance of a first lien promissory note payable to": False}})
        self.assertEqual(keys(r), ["la_no_note"])

    def test_adjustment_boxes(self):
        both = assumption(la={"checks": {"Sales Price will be adjusted by the amount": True}})
        self.assertEqual(keys(both), ["la_adjust_conflict"])
        none = assumption(la={"checks": {"cash payable at closing": False}})
        self.assertEqual(keys(none), ["la_adjust_blank"])

    def test_release_not_listed_is_warning_only(self):
        r = assumption(contract={"checks": {**ASSUME_ONLY["checks"], "Third Party Financing Addendum": False}})
        self.assertEqual(keys(r), ["la_release_not_listed"])
        self.assertTrue(r["complete"])

    def test_address_and_initials(self):
        r = assumption(la={"text": {"Address of Property": "99 Oak Ave, Austin, TX", "and Seller": "", "Text3": ""}})
        self.assertEqual(sorted(keys(r)), ["initials_seller", "la_address_mismatch"])

    def test_40_11_is_not_mistaken_for_41_3(self):
        r = check_tc_file([CLEAN, CLEAN_FA])
        self.assertFalse(r["has_loan_assumption"])
        self.assertTrue(r["has_addendum"])


if __name__ == "__main__":
    unittest.main()
