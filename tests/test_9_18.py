"""TREC 9-18 Unimproved Property rules (trec_9_18.py), per txo-trec-qa's
spec_9-18_25-17.md section 6 (9-18 rows).

Each planted file is the clean two-buyer/two-seller 9-18 with one change
and must fire exactly its key; the clean file + its 40-11 must give zero.

    python3 -m unittest tests.test_9_18
"""
import io
import os
import re
import unittest
from unittest import mock

import fitz

import trec_9_18
from tc_audit import check_tc_file
from tests.trec_9_18_fixtures import ADDRESS, BLANK, build, build_40_11

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.expanduser("~/txtanoffer-team/work/txo-trec-qa/spec_9-18_25-17.md")
NO_HEADERS = {f"header_p{p}": " " for p in range(2, 12)}


def keys(result):
    return [i["key"] for i in result["issues"]]


class FieldMap(unittest.TestCase):
    def test_every_role_is_on_its_page_at_its_rect(self):
        """MAP is spec 2a's table; this re-checks each row against TREC's
        own 9-18_2.pdf so a transcription slip can't read the wrong blank."""
        doc = fitz.open(BLANK)
        self.assertEqual(sum(len(list(p.widgets())) for p in doc), 291)
        where = {}
        for page_no, page in enumerate(doc, 1):
            h = page.rect.height
            for w in page.widgets():
                r = w.rect
                where.setdefault(w.field_name, []).append((page_no, (r.x0, h - r.y1, r.x1, h - r.y0)))
        for role, (page_no, name, rect) in trec_9_18.MAP.items():
            with self.subTest(role):
                self.assertEqual(len(where[name]), 1)
                got_page, got_rect = where[name][0]
                self.assertEqual(got_page, page_no)
                for a, b in zip(got_rect, rect):
                    self.assertAlmostEqual(a, b, delta=1.0)
        for page_no, name in trec_9_18.HEADER_FIELDS:
            with self.subTest(header=page_no):
                self.assertEqual([p for p, _ in where[name]], [page_no])

    @unittest.skipUnless(os.path.exists(SPEC), "spec not on this machine")
    def test_map_matches_spec_table(self):
        sec = open(SPEC).read().split("### 2a.")[1].split("### 2b.")[0]
        rows = re.findall(r"^\| `(\w+)` \| \d+ \| (\d+) \| [\d., ]+ \| `([^`]+)` \|", sec, re.M)
        self.assertEqual({r: (int(p), n) for r, p, n in rows},
                         {r: (p, n) for r, (p, n, _) in trec_9_18.MAP.items()})
        headers = re.findall(r"^- p(\d+): `([^`]+)`", sec.split("**Initials footer quads**")[0], re.M)
        self.assertEqual([(int(p), n) for p, n in headers], trec_9_18.HEADER_FIELDS)

    def test_never_reads_the_20_19_or_25_17_map(self):
        src = open(trec_9_18.__file__).read()
        self.assertNotIn("FIELD_MAP", src.split("MAP = {")[1])
        self.assertNotIn("F.MAP", src)


class ConsequenceTags(unittest.TestCase):
    def test_every_9_18_key_has_a_tag(self):
        """Every finding key trec_9_18 can emit shows a consequence tag in
        the report, not a bare MUST FIX / WARNING."""
        import tc_audit
        import tc_check_email
        src = open(trec_9_18.__file__).read()
        found = set(re.findall(r'"([a-z][a-z0-9_]+)"\)\s*$', src, re.M)) | set(re.findall(r'none_key="([a-z_]+)"', src))
        found -= set(trec_9_18.MAP) - {"county", "buyer_name", "seller_name"}
        # Emitted through loops and the shared trec_25_17 / tc_audit helpers.
        found |= {"escrow_agent_name", "earnest_money_amount", "option_fee_amount", "title_company", "initials_buyer",
                  "initials_seller", "initials_mismatch", "outdated_form", "header_address", "header_address_blank",
                  "header_address_differs", "financing_addendum_missing", "loan_amount_mismatch",
                  "addendum_checkbox_mismatch", "la_checkbox_mismatch", "loan_assumption_addendum_missing",
                  "extra_file_unrecognized", tc_audit.AMEND_PRICE_CHANGE_KEY, tc_audit.AMEND_ADDRESS_MISMATCH_KEY}
        self.assertIn("nrl_option_missing", found)
        self.assertEqual(sorted(k for k in found if k not in tc_check_email._CONSEQUENCE_TAGS), [])

    def test_no_tag_names_a_paragraph_the_9_18_lacks(self):
        import tc_check_email
        self.assertNotIn("2A", tc_check_email._CONSEQUENCE_TAGS["header_address_differs"])


class CheckedFormsLine(unittest.TestCase):
    """One constant for every "TC Check checks ..." line, so it can't drift."""

    def test_line(self):
        import trec_forms
        self.assertEqual(trec_forms.checked_contracts_line(), "TREC 20-19, TREC 25-17, TREC 9-18 and Colorado CBS1")

    def test_every_built_in_checker_is_listed(self):
        import tc_audit
        import trec_forms
        for fid in trec_forms.CURRENT_FORMS:
            with self.subTest(fid):
                listed = f"TREC {fid}" in trec_forms.CHECKED_CONTRACTS
                self.assertEqual(listed, fid == "20-19" or tc_audit._builtin_checker(fid) is not None)

    def test_email_and_web_use_it(self):
        import app as app_module
        import tc_check_email
        r = check_tc_file([os.path.join(ROOT, "tests", "fixtures", "trec_9-16_flat.pdf")])
        line = "TC Check checks TREC 20-19, TREC 25-17, TREC 9-18 and Colorado CBS1 contracts today."
        self.assertIn(line, tc_check_email.format_reply_body(r))
        self.assertIn(line, tc_check_email.format_reply_html(r))
        html = app_module.app.test_client().get("/tc-check", headers={"User-Agent": "Mozilla/5.0 test"}).get_data(as_text=True)
        self.assertIn(f"'<p>{line}</p>'", html)
        self.assertNotIn("__CHECKED_CONTRACTS__", html)


class Controls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clean, cls.fa = build(), build_40_11()

    def test_clean_alone_only_financing_addendum_missing(self):
        r = check_tc_file([self.clean])
        self.assertEqual(keys(r), ["financing_addendum_missing"])
        self.assertEqual(r["form"], "TREC 9-18")

    def test_clean_plus_40_11_zero(self):
        r = check_tc_file([self.clean, self.fa])
        self.assertEqual(r["issues"], [])
        self.assertTrue(r["recognized"] and r["complete"] and r["has_addendum"])
        self.assertEqual(r["property"], {"address": ADDRESS, "city": "Bastrop", "county": "Bastrop"})

    def test_order_does_not_matter(self):
        self.assertEqual(check_tc_file([self.fa, self.clean])["issues"], [])

    def test_effective_date_uses_9_18_roles(self):
        # '20_2' is the YEAR here (25-17: the month).
        from pdf_validator import _read_values
        import trec_25_17
        f = trec_25_17._Form(_read_values(self.clean), {}, trec_9_18.MAP)
        self.assertEqual(str(f.effective_date()), "2026-10-20")

    def test_seller_is_idx_0_and_initials_are_read(self):
        # Initials RS/LS are the Seller's (idx 0). And every page 1-8 footer
        # is actually read: one blank box on page 8 fires.
        self.assertNotIn("initials_mismatch", keys(check_tc_file([self.clean, self.fa])))
        r = check_tc_file([build("p8", initials_p8_buyer1=" "), self.fa])
        self.assertEqual([(i["key"], i["message"]) for i in r["issues"]],
                         [("initials_buyer", "Page 8 of 11: Buyer initials missing (1 of 2)")])


class Planted(unittest.TestCase):
    """One change each; exactly these keys (and severities) fire. Numbers
    are spec section 6's planted table."""

    @classmethod
    def setUpClass(cls):
        cls.fa = build_40_11()

    def check(self, expect, fa=None, **changes):
        r = check_tc_file([build("planted", **changes), fa or self.fa])
        got = sorted((i["key"], i["severity"]) for i in r["issues"])
        self.assertEqual(got, sorted(expect), [i["message"] for i in r["issues"]])
        return r

    def test_01_price_math(self):
        self.check([("sales_price_math", "blocker")], sales_price_3c="201,000.00")

    def test_02_closing_not_a_date(self):
        self.check([("closing_date_invalid", "blocker")], closing_date="November 31")

    def test_03_closing_before_effective(self):
        self.check([("closing_before_effective", "blocker")], closing_date="October 1")

    def test_04_option_days_blank(self):
        self.check([("option_days_blank", "blocker")], option_days=" ")

    def test_05_header_address(self):
        r = self.check([("header_address", "blocker")], header_p4="125 Main St, Bastrop, TX 78602")
        self.assertEqual(r["issues"][0]["message"], 'Page 4 of 11: header address reads "125 Main St, Bastrop, TX 78602" '
                                                    'but Paragraph 2 says "123 Main St, Bastrop, TX 78602"')

    def test_06_initials_missing(self):
        r = self.check([("initials_seller", "blocker")], initials_p3_seller2=" ")
        self.assertIn("Page 3 of 11", r["issues"][0]["message"])

    def test_07_receipt(self):
        r = self.check([("receipt_mismatch", "blocker")], rcpt_option="250.00")
        self.assertIn("(page 11)", r["issues"][0]["message"])

    def test_08_40_11_principal(self):
        self.check([("loan_amount_mismatch", "blocker")], fa=build_40_11("off", "150,000.00"))

    def test_11_city_blank_is_fine(self):
        self.check([], city=" ")

    def test_12_address_blank_with_description(self):
        self.check([], address=" ", **NO_HEADERS)

    def test_13_no_description(self):
        r = self.check([("property_description_blank", "warning")], lot=" ", block=" ", addition=" ", address=" ",
                       county=" ", **NO_HEADERS)
        self.assertTrue(r["issues"][0]["message"].startswith("Paragraph 2: no lot, block, addition or address"))

    def test_county_blank(self):
        self.check([("county", "blocker")], county=" ")

    def test_14_3d_will_and_will_not(self):
        self.check([("check_one_conflict", "blocker")], adj_will=True)

    def test_15_3d_will_acres_blank(self):
        self.check([("acreage_adjustment_incomplete", "blocker")], adj_will_not=False, adj_will=True,
                   adj_per_acre="5,000", adj_to_3a=True, adj_term_days="5")

    def test_3d_will_without_target_or_days(self):
        self.check([("acreage_adjustment_review", "warning")] * 2, adj_will_not=False, adj_will=True, adj_acres="10",
                   adj_per_acre="5,000")

    def test_3d_will_not_with_acres(self):
        self.check([("acreage_adjustment_conflict", "warning")], adj_acres="10")

    def test_17_title_expense_both(self):
        self.check([("check_one_conflict", "blocker")], title_exp_buyer=True)

    def test_title_expense_none(self):
        self.check([("title_expense_blank", "warning")], title_exp_seller=False)

    def test_6a8_ii_without_payer(self):
        self.check([("check_one_conflict", "blocker")], shortages_i=False, shortages_ii=True)

    def test_18_survey_two_options(self):
        self.check([("check_one_conflict", "blocker")], survey_1=True, survey_1_days="10", survey_1_new_seller=True)

    def test_19_survey_days_blank(self):
        self.check([("survey_days_blank", "blocker")], survey_2_days=" ")

    def test_survey_none(self):
        self.check([("survey_box_missing", "warning")], survey_2=False, survey_2_days=" ")

    def test_objection_days_blank(self):
        self.check([("objection_days_blank", "warning")], objection_days=" ")

    def test_poa_is_without_addendum(self):
        self.check([("poa_addendum_missing", "warning")], poa_is_not=False, poa_is=True)

    def test_tadd_conflict(self):
        self.check([("check_one_conflict", "blocker")], tadd_is=True)

    def test_20_7e_is_and_is_not(self):
        self.check([("check_one_conflict", "blocker")], disc3_is=True)

    def test_21_7e_is_without_explanation(self):
        self.check([("disclosure_explain_blank", "warning")], disc1_not=False, disc1_is=True)

    def test_7e_explanation_on_line_2_counts(self):
        self.check([], disc1_not=False, disc1_is=True, disc_explain_2="Low spot floods in heavy rain")

    def test_7e7_is_without_wholly_or_partly(self):
        self.check([("disclosure_incomplete", "warning")], disc7_not=False, disc7_is=True,
                   disc_explain_2="Northeast corner")

    def test_7e7_wholly_and_partly(self):
        self.check([("check_one_conflict", "blocker")], disc7_not=False, disc7_is=True, disc7_wholly=True,
                   disc7_partly=True, disc_explain_2="Northeast corner")

    def test_22_water_none(self):
        # Must-fix (Phanel approved 2026-10-25).
        self.check([("water_disclosure_box_missing", "blocker")], water_1=False)

    def test_23_water_two(self):
        self.check([("check_one_conflict", "blocker")], water_3=True, water_provider_1="City of Bastrop")

    def test_7f2_has_no_days_rule(self):
        # 7F(2)'s "Within ___ days" has no field on the 9-18 (spec 2a).
        self.check([], water_1=False, water_2=True)

    def test_7f3_without_provider(self):
        self.check([("water_provider_blank", "warning")], water_1=False, water_3=True)

    def test_25_4b_is_neither_option(self):
        self.check([("nrl_option_missing", "blocker")], nrl_is_not=False, nrl_is=True)

    def test_4b_is_and_is_not(self):
        self.check([("check_one_conflict", "blocker")], nrl_is=True, nrl_1=True)

    def test_4b_both_options(self):
        self.check([("check_one_conflict", "blocker")], nrl_is_not=False, nrl_is=True, nrl_1=True, nrl_2=True,
                   nrl_days="10")

    def test_4b_is_not_with_option(self):
        self.check([("lease_conflict", "warning")], nrl_1=True)

    def test_4b2_days_blank(self):
        self.check([("lease_incomplete", "warning")], nrl_is_not=False, nrl_is=True, nrl_2=True)

    def test_27_reservation(self):
        # As on the 25-17: the shared Paragraph 11 regex doesn't match this
        # sentence, so special_provisions_business_term doesn't fire.
        r = self.check([("reservation_without_addendum", "warning")], special_prov_1="Seller reserves 1/2 of the minerals")
        self.assertTrue(r["issues"][0]["message"].startswith("Paragraph 2 says any reservation"))

    def test_27_reservation_listed_in_22(self):
        self.check([], special_prov_1="Seller reserves 1/2 of the minerals", p22_mineral_reservation=True)

    def test_28_propane_is_not_a_reservation(self):
        self.check([], special_prov_1="Propane gas tank conveys")

    def test_31_7b_as_is_none_is_fine(self):
        self.check([], asis_1=False)

    def test_7b_as_is_both(self):
        self.check([("check_one_conflict", "blocker")], asis_2=True, repairs_1="Clear brush from the driveway")

    def test_32_appraisal_without_tpf(self):
        r = check_tc_file([build("p32", p22_appraisal=True, p22_tpf=False, tpf_3b_box=False, financing_3b="0",
                                 cash_3a="200,000.00")])
        self.assertEqual(keys(r), ["appraisal_without_tpf"])

    def test_12b_dollar_blank(self):
        self.check([("broker_contribution_incomplete", "blocker")], b12_1_row=True, b12_1_dollar_box=True)

    def test_effective_date_blank(self):
        r = self.check([("effective_date", "blocker")], eff_year=" ")
        self.assertEqual(r["issues"][0]["message"], "Page 9 of 11: Effective Date is blank")

    def test_invalid_email(self):
        self.check([("email_invalid", "warning")], n_buyer_email="john.buyer@")

    def test_41_3_listed_not_attached(self):
        self.check([("loan_assumption_addendum_missing", "blocker")], p22_loan_assumption=True)

    def test_rural_road_spelled_two_ways(self):
        addr = "4501 Farm to Market Road 969, Bastrop, TX 78602"
        self.check([], address=addr, **{f"header_p{p}": "4501 FM 969, Bastrop, TX 78602" for p in range(2, 12)})

    def test_legal_description_headers_are_review_only(self):
        r = self.check([("header_address_differs", "warning")] * 10,
                       **{f"header_p{p}": "Lot 7, Block B, Pine Ridge" for p in range(2, 12)})
        self.assertEqual(r["issues"][0]["message"],
                         'Page 2 of 11: the header reads "Lot 7, Block B, Pine Ridge" and Paragraph 2\'s address '
                         'is "123 Main St, Bastrop, TX 78602". Confirm the header identifies this property.')


class WebRoute(unittest.TestCase):
    def test_upload_returns_9_18_report(self):
        import app as app_module
        with open(build("web", option_days=" "), "rb") as a, open(build_40_11("web"), "rb") as b:
            data = {"file": [(io.BytesIO(a.read()), "land.pdf"), (io.BytesIO(b.read()), "40-11.pdf")],
                    "source_page": "claude_verify"}
        with mock.patch("app.maybe_send_unreadable_alert"), mock.patch("app.maybe_send_real_upload_alert"), \
                mock.patch("app.track_event"):
            r = app_module.app.test_client().post("/v1/tc/check", data=data, content_type="multipart/form-data",
                                                  headers={"User-Agent": "Mozilla/5.0 test"}).get_json()
        self.assertTrue(r["recognized"])
        self.assertEqual(r["form"], "TREC 9-18")
        self.assertEqual(r["files_line"], "TREC 9-18 + 40-11 addendum")
        self.assertEqual([i["message"] for i in r["display_issues"]],
                         ["Paragraph 5B: Option fee is entered but the option period (number of days) is blank"])

    def test_email_names_section_2_when_nothing_describes_the_property(self):
        import tc_check_email
        r = check_tc_file([build("nodesc", lot=" ", block=" ", addition=" ", address=" ", city=" ", **NO_HEADERS),
                           build_40_11()])
        self.assertIn("Property address not filled in (Section 2) ", tc_check_email.format_reply_body(r))
        self.assertIn("Section 2 is blank", tc_check_email.format_reply_html(r))


class Flattened(unittest.TestCase):
    def test_flattened_9_18_is_unreadable_not_all_blank(self):
        doc = fitz.open(build("flat"))
        doc.bake()
        path = os.path.join(os.path.dirname(build("tmp")), "flat_9-18.pdf")
        doc.save(path)
        r = check_tc_file([path])
        self.assertFalse(r["recognized"])
        self.assertTrue(r["unreadable"])
        self.assertEqual(keys(r), ["unrecognized"])
        self.assertIn("TREC 9-18", r["issues"][0]["message"])


class Routing(unittest.TestCase):
    def test_9_18_never_reaches_20_19_or_25_17_rules(self):
        trap = mock.Mock(side_effect=AssertionError("other form's rules ran"))
        with mock.patch("tc_audit._consistency_issues", trap), mock.patch("tc_audit._contract_pages", trap), \
                mock.patch("trec_25_17._issues", trap), mock.patch("trec_25_17.check_25_17", trap):
            r = check_tc_file([build("routing", option_days=" "), build_40_11()])
        self.assertEqual(r["form"], "TREC 9-18")
        self.assertEqual(keys(r), ["option_days_blank"])


if __name__ == "__main__":
    unittest.main()
