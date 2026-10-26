"""TREC 30-18 Residential Condominium rules (trec_30_18.py), per txo-trec-qa's
spec_30-18.md section 6, ported from its prototype tests.

Each planted file is the clean two-buyer/two-seller 30-18 with one change
and must fire exactly its key; the clean file + its 40-11 must give zero.

    python3 -m unittest tests.test_30_18
"""
import os
import re
import unittest
from unittest import mock

import fitz

import trec_30_18
from tc_audit import check_tc_file
from tests.trec_30_18_fixtures import BLANK, ROOT, build, build_30_17, build_36_11, build_40_11, strip_widgets


def keys(r):
    return [i["key"] for i in r["issues"]]


class FieldMap(unittest.TestCase):
    def test_every_role_is_on_its_page_at_its_rect(self):
        doc = fitz.open(BLANK)
        self.assertEqual(sum(len(list(p.widgets())) for p in doc), 256)
        where = {}
        for page_no, page in enumerate(doc, 1):
            h = page.rect.height
            for w in page.widgets():
                r = w.rect
                where.setdefault(w.field_name, []).append((page_no, (r.x0, h - r.y1, r.x1, h - r.y0)))
        for role, (page_no, name, rect) in trec_30_18.MAP.items():
            with self.subTest(role):
                self.assertEqual(len(where[name]), 1)
                self.assertEqual(where[name][0][0], page_no)
                for a, b in zip(where[name][0][1], rect):
                    self.assertAlmostEqual(a, b, delta=1.0)
        for page_no, name in trec_30_18.HEADER_FIELDS:
            with self.subTest(header=page_no):
                self.assertEqual([p for p, _ in where[name]], [page_no])


class ConsequenceTags(unittest.TestCase):
    def test_every_30_18_key_has_a_tag(self):
        import tc_audit
        import tc_check_email
        src = open(trec_30_18.__file__).read()
        found = (set(re.findall(r'"([a-z][a-z0-9_]+)"\)\s*$', src, re.M)) | set(re.findall(r'none_key="([a-z_]+)"', src))
                 | set(re.findall(r'"key": "([a-z_]+)"', src)))
        found -= set(trec_30_18.MAP) - {"county", "address", "buyer_name", "seller_name"}
        found |= {"escrow_agent_name", "earnest_money_amount", "option_fee_amount", "title_company", "initials_buyer",
                  "initials_seller", "initials_mismatch", "outdated_form", "financing_addendum_missing",
                  "loan_amount_mismatch", "addendum_checkbox_mismatch", "la_checkbox_mismatch",
                  "loan_assumption_addendum_missing", "extra_file_unrecognized", tc_audit.AMEND_PRICE_CHANGE_KEY,
                  tc_audit.AMEND_ADDRESS_MISMATCH_KEY}
        for k in ("condo_docs_box_missing", "resale_cert_days_blank", "poa_addendum_on_condo", "header_address"):
            self.assertIn(k, found)
        self.assertEqual(sorted(k for k in found if k not in tc_check_email._CONSEQUENCE_TAGS), [])


class Controls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clean, cls.fa = build(), build_40_11()

    def test_clean_alone_only_financing_addendum_missing(self):
        r = check_tc_file([self.clean])
        self.assertEqual(keys(r), ["financing_addendum_missing"])
        self.assertEqual(r["form"], "TREC 30-18")

    def test_clean_plus_40_11_zero(self):
        r = check_tc_file([self.clean, self.fa])
        self.assertEqual(r["issues"], [])
        self.assertTrue(r["recognized"] and r["complete"] and r["has_addendum"])
        self.assertEqual(r["property"], {"address": "123 Main St, Unit 12, 78701", "city": "Austin", "county": "Travis"})

    def test_order_does_not_matter(self):
        self.assertEqual(check_tc_file([self.fa, self.clean])["issues"], [])

    def test_effective_date_uses_30_18_roles(self):
        # '20_2' is the MONTH here (20-19: the year).
        from pdf_validator import _read_values
        self.assertEqual(str(trec_30_18._Form(_read_values(self.clean), {}).effective_date()), "2026-10-20")

    def test_page_1_initials_are_read(self):
        # pypdf reads page 1 as "Page    of 10" (no number); the fallback
        # in contract_pages must still find it.
        r = check_tc_file([build("p1", initials_p1_buyer2=" "), self.fa])
        self.assertEqual([(i["key"], i["message"]) for i in r["issues"]],
                         [("initials_buyer", "Page 1 of 10: Buyer initials missing (1 of 2)")])

    def test_never_reaches_20_19_rules(self):
        self.assertFalse({"city", "disclosure_days_blank", "poa_addendum_missing"} & set(keys(check_tc_file([build("x", sdn_1=True)]))))


class Planted(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.fa = build_40_11()
    def check(self, expect, fa=None, files=None, **changes):
        r = check_tc_file([build("planted", **changes), fa or self.fa] + (files or []))
        got = sorted((i["key"], i["severity"]) for i in r["issues"])
        self.assertEqual(got, sorted(expect), [i["message"] for i in r["issues"]]); return r
    # carried over from 20-19
    def test_01_price_math(self): self.check([("sales_price_math", "blocker")], sales_price_3c="426,000.00")
    def test_02_closing_not_a_date(self): self.check([("closing_date_invalid", "blocker")], closing_date="November 31")
    def test_03_closing_before_effective(self): self.check([("closing_before_effective", "blocker")], closing_date="October 1")
    def test_04_option_days_blank(self): self.check([("option_days_blank", "blocker")], option_days=" ")
    def test_05_header_address(self):
        r = self.check([("header_address", "blocker")], header_p4="125 Main St, Unit 12, Austin, TX 78701")
        self.assertIn("Page 4 of 10", r["issues"][0]["message"])
    def test_06_initials_missing(self): self.check([("initials_seller", "blocker")], initials_p3_seller2=" ")
    def test_07_receipt(self): self.check([("receipt_mismatch", "blocker")], rcpt_option="300.00")
    def test_08_40_11_principal(self): self.check([("loan_amount_mismatch", "blocker")], fa=build_40_11("p8", principal="370,000.00"))
    def test_09_county_blank(self): self.check([("county", "blocker")], county=" ")
    def test_10_address_blank(self): self.check([("address", "blocker")], address=" ")
    def test_11_escrow_agent_blank(self): self.check([("escrow_agent_name", "blocker")], escrow_agent=" ")
    def test_12_title_company_blank(self): self.check([("title_company", "blocker")], title_company=" ")
    def test_13_effective_date_blank(self): self.check([("effective_date", "blocker")], eff_month=" ")
    def test_14_7b_none(self): self.check([("disclosure_box_missing", "blocker")], sdn_1=False)
    def test_15_7b_two(self): self.check([("check_one_conflict", "blocker")], sdn_2=True, sdn_days="3")
    def test_16_7b2_days_blank(self): self.check([("disclosure_days_blank", "blocker")], sdn_1=False, sdn_2=True, sdn_days=" ")
    def test_17_7d_two(self): self.check([("check_one_conflict", "blocker")], asis_2=True, repairs_1="Replace water heater")
    def test_18_10a_two(self): self.check([("check_one_conflict", "blocker"), ("possession_lease_not_listed", "warning")], poss_lease=True)
    def test_19_10a_lease_not_listed(self): self.check([("possession_lease_not_listed", "warning")], poss_closing=False, poss_lease=True)
    def test_20_6a_both(self): self.check([("check_one_conflict", "blocker")], title_exp_buyer=True)
    def test_21_6a_none(self): self.check([("title_expense_blank", "warning")], title_exp_seller=False)
    def test_22_12b_row_only(self): self.check([("broker_contribution_incomplete", "blocker")], b12_1_row=True)
    def test_23_email(self): self.check([("email_invalid", "warning")], n_buyer_email="john at example")
    def test_24_special_provisions(self): self.check([("special_provisions_business_term", "warning")], special_prov_1="Seller to pay $2,000 of Buyer's closing costs")
    def test_25_4a_not_listed(self): self.check([("lease_addendum_not_listed", "warning")], lease_4a_res=True)
    def test_26_objection_days(self): self.check([("objection_days_blank", "warning")], objection_days=" ")
    def test_27_appraisal_without_tpf(self):
        r = check_tc_file([build("p27", cash_3a="425,000.00", financing_3b=" ", tpf_3b_box=False, p22_tpf=False, p22_appraisal=True)])
        self.assertEqual(sorted((i["key"], i["severity"]) for i in r["issues"]), [("appraisal_without_tpf", "warning")])
    # condo-specific
    def test_28_docs_none(self): self.check([("condo_docs_box_missing", "blocker")], docs_2=False)
    def test_29_docs_both(self): self.check([("check_one_conflict", "blocker")], docs_1=True)
    def test_30_docs_days_blank(self): self.check([("condo_docs_days_blank", "blocker")], docs_days=" ")
    def test_31_docs_1_days_ignored(self): self.check([], docs_1=True, docs_2=False, docs_days=" ")
    def test_32_cert_none(self): self.check([("resale_cert_box_missing", "blocker")], cert_2=False)
    def test_33_cert_two(self): self.check([("check_one_conflict", "blocker")], cert_3=True)
    def test_34_cert_days_blank(self): self.check([("resale_cert_days_blank", "blocker")], cert_days=" ")
    def test_35_cert_3_waiver_is_fine(self): self.check([], cert_2=False, cert_3=True, cert_days=" ")
    def test_36_unit_in_address_only(self): self.check([], unit=" ")
    def test_37_unit_nowhere(self):
        self.check([("condo_unit_blank", "warning")], unit=" ", address="123 Main St, 78701",
                   **{f"header_p{p}": "123 Main St, Austin, TX 78701" for p in range(2, 11)})
    def test_38_project_name_blank(self): self.check([("condo_project_blank", "warning")], condo_name=" ")
    def test_39_city_blank(self): self.check([("condo_city_blank", "warning")], city=" ")
    def test_40_assoc_fee_cap_blank(self): self.check([("assoc_fee_cap_blank", "warning")], assoc_fee_cap=" ")
    def test_41_header_hash_unit_same(self): self.check([], header_p5="123 Main St #12, Austin")
    def test_42_header_other_unit(self): self.check([("header_address", "blocker")], header_p5="123 Main St Unit 14, Austin")
    def test_43_header_without_unit(self): self.check([], header_p5="123 Main Street, Austin, TX")
    def test_44_header_project_name(self): self.check([("header_address_differs", "warning")], header_p5="Main Street Lofts Unit 12")
    def test_45_36_11_attached(self): self.check([("poa_addendum_on_condo", "warning")], files=[build_36_11()])
    def test_46_seller_idx0_trap(self): self.assertNotIn("initials_mismatch", keys(check_tc_file([build(), self.fa])))



class Routing(unittest.TestCase):
    def _r(self, path):
        return [(i["key"], i["severity"]) for i in check_tc_file([path])["issues"]]

    def test_never_reaches_other_forms_rules(self):
        trap = mock.Mock(side_effect=AssertionError("another form's rules ran"))
        with mock.patch("tc_audit._consistency_issues", trap), mock.patch("tc_audit._contract_pages", trap), \
                mock.patch("trec_25_17._issues", trap), mock.patch("trec_9_18._issues", trap):
            r = check_tc_file([build("routing", option_days=" "), build_40_11()])
        self.assertEqual(r["form"], "TREC 30-18")
        self.assertEqual(keys(r), ["option_days_blank"])

    def test_30_17_after_mandatory(self):
        self.assertEqual(self._r(build_30_17("a", "20", "October", "26")), [("outdated_form", "blocker")])

    def test_30_17_before_mandatory(self):
        r = check_tc_file([build_30_17("b", "15", "May", "26")])
        self.assertEqual([(i["key"], i["severity"]) for i in r["issues"]], [("outdated_form", "info")])
        self.assertIn("May 15, 2026", r["issues"][0]["message"])

    def test_30_17_blank(self):
        self.assertEqual(self._r(build_30_17("c", " ", " ", " ")), [("outdated_form", "blocker")])

    def test_30_16_flat(self):
        self.assertEqual(self._r(os.path.join(ROOT, "tests", "fixtures", "trec_30-16_flat.pdf")), [("outdated_form", "blocker")])

    def test_widgets_stripped_is_unreadable(self):
        # spec 0.2: names left in /Fields, no widgets on the pages.
        r = check_tc_file([strip_widgets(build("flat"), "30-18")])
        self.assertTrue(r["unreadable"])
        self.assertEqual(keys(r), ["unrecognized"])
        self.assertIn("TREC 30-18", r["issues"][0]["message"])


if __name__ == "__main__":
    unittest.main()
