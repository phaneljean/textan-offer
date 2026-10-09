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


if __name__ == "__main__":
    unittest.main()
