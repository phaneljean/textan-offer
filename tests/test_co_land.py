"""Colorado CBS4 Contract to Buy and Sell Real Estate (Land), 2026 edition:
co_land.py, txo-trec-qa spec_co_land.md. A clean two-party deal gives no
findings; each planted error fires exactly its own key.

    python3 -m unittest tests.test_co_land
"""
import os
import tempfile
import unittest

import fitz

import tc_check_email
from tc_audit import check_tc_file
from tests.co_land_fixtures import fill

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
SPD_BLANK = os.path.join(FIX, "CO_SPD19L_blank_2026.pdf")

# (name, widget overrides, expected [(key, severity)]) -- spec §5 table.
PLANTED = [
    ("cash_wrong", {92: "80,000.00"}, [("co_price_math", "blocker")]),
    ("county_blank", {9: " "}, [("co_county_blank", "blocker")]),
    ("legal_blank_street_kept", {10: " "}, [("co_legal_blank", "warning")]),
    ("legal_and_street_blank", {10: " ", 11: " "}, [("co_land_property_blank", "blocker")]),
    ("city_blank", {12: " "}, []),
    ("closing_thanksgiving", {79: "11/26/2026", 80: "11/26/2026"}, [("co_closing_holiday", "warning")]),
    ("dd_resolution_after_closing", {72: "11/05/2026"}, [("co_after_closing_dd_res", "blocker")]),
    ("both_residence_boxes", {1: True}, [("co_conflict_no_res", "blocker")]),
    ("lead_paint_blank_vacant", {48: " ", 76: " "}, []),
    # §2.7.1's box is a second widget of "Solar Power Will Checkbox": with
    # §2.5.5 Will Not checked it must not read as a solar conflict.
    ("deeded_water_shared_field", {26: True, 24: True, 28: "special warranty"},
     [("co_land_deeded_water_blank", "warning")]),
    ("water_exam_blank", {64: " "}, [("co_land_water_review_deleted", "warning")]),
    ("water_exam_na", {64: "N/A"}, []),
    ("mineral_exam_blank", {65: " "}, [("co_land_mineral_review_deleted", "warning")]),
    ("ilc_and_survey", {139: True}, [("co_conflict_new_ilc", "blocker")]),
    ("survey_form_blank", {141: " "}, [("co_land_survey_form_blank", "warning")]),
    ("well_and_no_well", {177: True, 178: False, 179: True}, [("co_land_well_conflict", "warning")]),
    ("two_deeds", {184: True}, [("co_conflict_deed", "blocker")]),
    ("water_transfer_two_payers", {226: True, 227: True}, [("co_conflict_payer_wtf", "blocker")]),
    ("conditional_sale_no_deadline", {174: "456 Oak St, Denver", 75: " "},
     [("co_land_conditional_sale_deadline_blank", "warning")]),
    ("loan_type_unchecked", {100: False}, [("co_land_loan_type_unchecked", "warning")]),
]


def _spd(path, permit):
    doc = fitz.open(SPD_BLANK)
    for page in doc:
        for w in page.widgets():
            if w.field_name in ("Property", "B1 Well Permit Number"):
                w.field_value = "123 Main St, Kiowa, CO 80117" if w.field_name == "Property" else permit
                w.update()
    doc.save(path)
    doc.close()
    return path


class LandChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.clean = fill(out=cls.p("clean.pdf"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def p(cls, name):
        return os.path.join(cls.tmp.name, name)

    def found(self, *paths):
        r = check_tc_file(list(paths))
        self.assertEqual(r["form"], "Colorado CBS4 (Land)")
        return [(i["key"], i["severity"]) for i in r["issues"]]

    def test_clean_deal_is_clean(self):
        r = check_tc_file([self.clean])
        self.assertTrue(r["recognized"])
        self.assertTrue(r["complete"])
        self.assertEqual(r["issues"], [])
        self.assertEqual(r["property"], {"address": "123 Main St", "city": "Kiowa", "county": "Elbert"})

    def test_planted_errors(self):
        for name, overrides, expected in PLANTED:
            with self.subTest(name):
                self.assertEqual(self.found(fill(overrides, out=self.p(name + ".pdf"))), expected)

    def test_spd19l_well_permit_differs(self):
        spd = _spd(self.p("spd.pdf"), "999999")
        r = check_tc_file([self.clean, spd])
        self.assertEqual([(i["key"], i["severity"]) for i in r["issues"]], [("co_spd_well_permit_mismatch", "warning")])
        self.assertEqual(r["co_attachments"], ["Seller's Property Disclosure (Land)"])
        self.assertEqual(self.found(self.clean, _spd(self.p("spd_ok.pdf"), "123456")), [])

    def test_buyers_agent_without_agreement(self):
        self.assertEqual(self.found(fill({295: True}, out=self.p("agent.pdf"))), [("co_ertb_not_attached", "warning")])

    def test_residences_without_addendum(self):
        # Lead-paint rows are "N/A" in CLEAN, so only the addendum review fires.
        self.assertEqual(self.found(fill({0: False, 1: True}, out=self.p("res.pdf"))),
                         [("co_land_resadd_missing", "warning")])

    def test_flattened_land_is_unreadable_not_cbs1(self):
        doc = fitz.open(self.clean)
        for page in doc:
            for w in list(page.widgets()):
                page.delete_widget(w)
        doc.save(self.p("flat.pdf"))
        doc.close()
        r = check_tc_file([self.p("flat.pdf")])
        self.assertTrue(r["unreadable"])
        self.assertIn("Colorado CBS4 (Land)", r["issues"][0]["message"])

    def test_every_key_has_a_tag(self):
        import co_land
        import inspect
        import re
        keys = set(re.findall(r'"(co_[a-z0-9_]+)"', inspect.getsource(co_land)))
        keys |= {f"co_{k}_deleted" for k in ("loan_avail", "loan_terms")}
        keys.discard("co_attachments")
        untagged = [k for k in sorted(keys) if k not in tc_check_email._CONSEQUENCE_TAGS
                    and not any(k.startswith(p) for p, _ in tc_check_email._CONSEQUENCE_PREFIXES)]
        self.assertEqual(untagged, [])
