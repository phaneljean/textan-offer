"""Colorado contract routing (2026-10-26, txo-trec-qa spec_co_land.md §0.1).

co_audit.is_cbs1 counted CBS1 field names, 6 of 9 -- and the 2026 Land
(CBS4) contract carries 8 of them, the Commercial (CBS3) 6. Both got a full
CBS1 report with false must-fixes (a Land Cash at Closing field named
"Case At Closing" dropped $90,000 from the price math). Colorado contracts
are now identified by the printed page title first.

Fixtures (DRE, dre.colorado.gov/real-estate-broker-contracts-and-forms):
  CBS3_blank_2026.pdf     Commercial, 2026 edition    sha256 0b2e8b5b3f40c1d7...
  CBS4_6_24_blank.pdf     Land, CBS4-6-24 (outdated)  sha256 ada80c89b5c28c96...
  CBS4_blank_2026.pdf     Land, 2026 edition          sha256 6022d5e42bfa38ad...
tc_check_golden_cbs1.json is check_tc_file() output captured on 21f1b08
(before the routing change); CBS1 reports must stay byte-for-byte the same.

    python3 -m unittest tests.test_co_routing
    python3 tests/test_co_routing.py --update   # recapture the golden (only on purpose)
"""
import json
import os
import sys
import tempfile
import unittest

import fitz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tc_audit import check_tc_file  # noqa: E402
from tests.co_fixtures import fill, fill_form  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures")
GOLDEN = os.path.join(FIX, "tc_check_golden_cbs1.json")
HEADER = {"Original Contract Date": "10/01/2026", "Street Address": "123 Main St", "City": "Denver"}

# name -> (CBS1 overrides, [(companion kind, values)])
GOLDEN_CASES = {
    "clean": ({}, []),
    "blank_county_price_math": ({"County": " ", "Cash At Closing": "80,000.00"}, []),
    "deadline_after_closing_lead_paint": ({"Inspection Resolution Deadline": "11/05/2026",
                                           "Lead Based Paint Disclosure Deadline": " ",
                                           "LeadBased Paint Termination Deadline": " "}, []),
    "buyers_agent_no_ertb": ({"Broker Working With Buyer Buyers Agent Checkbox": True}, []),
    "with_ae41_and_swa": ({}, [("ae41", {**HEADER, "Additional Amendments Field": "Seller to leave the shed"}),
                               ("swa", {"Contract Date": "10/01/2026", "Street Address": "9 Elm St"})]),
    "blank_form": (None, []),
}


def build_case(name, tmp):
    overrides, companions = GOLDEN_CASES[name]
    paths = [os.path.join(FIX, "CBS1_blank_2026.pdf") if overrides is None
             else fill(overrides, out=os.path.join(tmp, f"{name}.pdf"))]
    for n, (kind, vals) in enumerate(companions):
        paths.append(fill_form(kind, vals, os.path.join(tmp, f"{name}_{n}_{kind}.pdf")))
    return paths


def fill_by_name(src, vals, out):
    """Fill any blank by exact field name (text only; checkboxes take True)."""
    doc = fitz.open(src)
    for page in doc:
        for w in page.widgets():
            if w.field_name in vals:
                v = vals[w.field_name]
                w.field_value = (w.on_state() if v else "Off") if w.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX else str(v)
                w.update()
    doc.save(out)
    doc.close()
    return out


class Cbs1Unchanged(unittest.TestCase):
    def test_golden_output_identical(self):
        with open(GOLDEN) as f:
            golden = json.load(f)
        with tempfile.TemporaryDirectory() as tmp:
            for name in GOLDEN_CASES:
                with self.subTest(name):
                    self.assertEqual(json.dumps(check_tc_file(build_case(name, tmp))), golden[name])


LAND_FIELDS = {"Date": "10/01/2026", "Buyer Name": "John Buyer", "Seller Name": "Robert Seller", "County": "Elbert",
               "Street Address": "123 Main St", "City": "Kiowa", "State": "CO", "Purchase Price": "400,000.00",
               "Earnest Money": "10,000.00", "New Loan": "300,000.00", "Case At Closing": "90,000.00",
               "Total 01": "400,000.00", "Total 02": "400,000.00", "Earnest Money Holder": "Example Title Co"}


class ColoradoRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = cls.tmp.name
        cls.land = fill_by_name(os.path.join(FIX, "CBS4_blank_2026.pdf"), LAND_FIELDS, os.path.join(t, "land.pdf"))
        cls.comm = fill_by_name(os.path.join(FIX, "CBS3_blank_2026.pdf"), {"Purchase Price": "900,000.00"},
                                os.path.join(t, "comm.pdf"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def assert_not_checked(self, r, form, name, code):
        self.assertEqual(r["form"], form)
        self.assertTrue(r["unsupported"])
        self.assertFalse(r["recognized"])
        self.assertEqual(r["state"], "CO")
        self.assertEqual([(i["key"], i["severity"]) for i in r["issues"]], [("form_not_supported", "info")])
        self.assertEqual(r["issues"][0]["message"],
                         f"We recognized the Colorado Contract to Buy and Sell Real Estate ({name}), {code}. "
                         f"TC Check doesn't check this form yet, so nothing was checked.")
        self.assertNotIn("TREC", r["issues"][0]["message"])

    def test_land_is_not_audited_as_cbs1(self):
        # Before: co_price_math ($310,000) and co_loan_type_missing, both false.
        self.assert_not_checked(check_tc_file([self.land]), "Colorado CBS4 (Land)", "Land", "CBS4")

    def test_commercial_is_not_audited_as_cbs1(self):
        self.assert_not_checked(check_tc_file([self.comm]), "Colorado CBS3 (Commercial)", "Commercial", "CBS3")

    def test_field_count_fallback_rejects_land_and_commercial(self):
        import co_audit
        from pdf_validator import _read_values
        self.assertFalse(co_audit.is_cbs1(_read_values(self.land)))
        self.assertFalse(co_audit.is_cbs1(_read_values(self.comm)))
        self.assertTrue(co_audit.is_cbs1(_read_values(os.path.join(FIX, "CBS1_blank_2026.pdf"))))

    def test_outdated_land_edition(self):
        old = os.path.join(FIX, "CBS4_6_24_blank.pdf")
        cases = {"10/01/2026": "blocker", "12/01/2025": "info", " ": "blocker"}
        for when, severity in cases.items():
            with self.subTest(when):
                r = check_tc_file([fill_by_name(old, {"Document Date": when}, os.path.join(self.tmp.name, "old.pdf"))])
                self.assertEqual(r["form"], "Colorado CBS4-6-24 (Land)")
                self.assertEqual([(i["key"], i["severity"]) for i in r["issues"]], [("co_outdated_form", severity)])
                self.assertNotIn("scanned", r["issues"][0]["message"])
        r = check_tc_file([old])
        self.assertIn("This file is on CBS4-6-24 (mandatory 8-24). The Commission's current Land contract is CBS4, "
                      "mandatory since January 1, 2026.", r["issues"][0]["message"])

    def test_outdated_email_says_colorado(self):
        import tc_check_email
        r = check_tc_file([os.path.join(FIX, "CBS4_6_24_blank.pdf")])
        self.assertEqual(tc_check_email.subject_line(r), "TC File Check: outdated Colorado form")
        self.assertNotIn("TREC", tc_check_email.format_reply_html(r).split("TC Check checks")[0])

    def test_compare_refuses_land(self):
        from tc_audit import compare_contracts
        r = compare_contracts(self.land, self.land)
        self.assertFalse(r["recognized"])
        self.assertIn("Colorado CBS4 (Land)", r["message"])


if __name__ == "__main__":
    if "--update" in sys.argv:
        with tempfile.TemporaryDirectory() as tmp:
            out = {name: json.dumps(check_tc_file(build_case(name, tmp))) for name in GOLDEN_CASES}
        with open(GOLDEN, "w") as f:
            json.dump(out, f, indent=1)
        print("wrote", GOLDEN)
    else:
        unittest.main()
