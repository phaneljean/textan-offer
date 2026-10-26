"""TREC 25-17 Farm and Ranch rules (trec_25_17.py), per txo-trec-qa's
spec_9-18_25-17.md section 6 (25-17 rows only; 9-18 is paused).

Each planted file is the clean two-buyer/two-seller 25-17 with one change
and must fire exactly its key; the clean file + its 40-11 must give zero.

    python3 -m unittest tests.test_25_17
"""
import os
import re
import unittest
from unittest import mock

import fitz

import trec_25_17
from tc_audit import check_tc_file
from tests.trec_25_17_fixtures import BLANK, build, build_40_11

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.expanduser("~/txtanoffer-team/work/txo-trec-qa/spec_9-18_25-17.md")


MUST_FIX_25_16 = ("This file is on TREC 25-16 (11-04-2024). TREC 25-17 (05-04-2026) is the current Farm and Ranch "
                  "Contract, mandatory since 07/01/2026. TC Check doesn't check replaced editions, so nothing else was "
                  "checked. If this contract isn't signed yet, write it on TREC 25-17.")


def keys(result):
    return [i["key"] for i in result["issues"]]


class FieldMap(unittest.TestCase):
    def test_every_role_is_on_its_page_at_its_rect(self):
        """MAP is spec 2b's table; this re-checks each row against TREC's
        own 25-17_4.pdf so a transcription slip can't read the wrong blank."""
        doc = fitz.open(BLANK)
        where = {}
        for page_no, page in enumerate(doc, 1):
            h = page.rect.height
            for w in page.widgets():
                r = w.rect
                where.setdefault(w.field_name, []).append((page_no, (r.x0, h - r.y1, r.x1, h - r.y0)))
        for role, (page_no, name, rect) in trec_25_17.MAP.items():
            with self.subTest(role):
                self.assertEqual(len(where[name]), 1)
                got_page, got_rect = where[name][0]
                self.assertEqual(got_page, page_no)
                for a, b in zip(got_rect, rect):
                    self.assertAlmostEqual(a, b, delta=1.0)

    @unittest.skipUnless(os.path.exists(SPEC), "spec not on this machine")
    def test_map_matches_spec_table(self):
        sec = open(SPEC).read().split("### 2b.")[1].split("### 2c")[0]
        rows = re.findall(r"^\| `(\w+)` \| \d+ \| (\d+) \| [\d., ]+ \| `([^`]+)` \|", sec, re.M)
        self.assertEqual({r: (int(p), n) for r, p, n in rows},
                         {r: (p, n) for r, (p, n, _) in trec_25_17.MAP.items()})


class ConsequenceTags(unittest.TestCase):
    def test_every_25_17_key_has_a_tag(self):
        """Every finding key trec_25_17 can emit shows a consequence tag in
        the report, not a bare MUST FIX / WARNING (txo-trec-qa M2)."""
        import tc_audit
        import tc_check_email
        src = open(trec_25_17.__file__).read()
        # Keys close their add()/one_of() call at the end of a line; MAP
        # role names read the same way (f.v("closing_date")) and are dropped.
        found = (set(re.findall(r'"([a-z][a-z0-9_]+)"\)\s*$', src, re.M)) | set(re.findall(r'none_key="([a-z_]+)"', src))
                 | set(re.findall(r'"key": "([a-z_]+)"', src)))   # header_issues builds its dicts directly
        found -= set(trec_25_17.MAP) - {"county", "buyer_name", "seller_name"}
        found.discard("unrecognized")   # unreadable_result's info line: the unreadable path, never a tagged finding
        # Emitted through loops and shared tc_audit helpers.
        found |= {"escrow_agent_name", "earnest_money_amount", "option_fee_amount", "title_company", "initials_buyer",
                  "initials_seller", "initials_mismatch", "outdated_form", tc_audit.AMEND_PRICE_CHANGE_KEY,
                  tc_audit.AMEND_ADDRESS_MISMATCH_KEY}
        self.assertIn("header_address_differs", found)
        self.assertEqual(sorted(k for k in found if k not in tc_check_email._CONSEQUENCE_TAGS), [])


class Controls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clean, cls.fa = build(), build_40_11()

    def test_clean_alone_only_financing_addendum_missing(self):
        r = check_tc_file([self.clean])
        self.assertEqual(keys(r), ["financing_addendum_missing"])
        self.assertEqual(r["form"], "TREC 25-17")

    def test_clean_plus_40_11_zero(self):
        r = check_tc_file([self.clean, self.fa])
        self.assertEqual(r["issues"], [])
        self.assertTrue(r["recognized"] and r["complete"] and r["has_addendum"])
        self.assertEqual(r["property"], {"address": "123 Main St, Giddings, TX 78942", "city": "", "county": "Lee"})

    def test_order_does_not_matter(self):
        self.assertEqual(check_tc_file([self.fa, self.clean])["issues"], [])

    def test_effective_date_uses_25_17_roles(self):
        # Planted #9: '20_2' is the MONTH here; read with 20-19 names this
        # would be year "October". The 25-17 map gives October 20, 2026.
        from pdf_validator import _read_values
        f = trec_25_17._Form(_read_values(self.clean), {})
        self.assertEqual(str(f.effective_date()), "2026-10-20")

    def test_seller_is_idx_0(self):
        # Planted #10: initials RS/LS are the Seller's (idx 0); a swapped
        # Buyer/Seller map would call every page a mismatch.
        self.assertNotIn("initials_mismatch", keys(check_tc_file([self.clean, self.fa])))


class Planted(unittest.TestCase):
    """One change each; exactly these keys (and severities) fire."""

    @classmethod
    def setUpClass(cls):
        cls.fa = build_40_11()

    def check(self, expect, fa=None, **changes):
        r = check_tc_file([build("planted", **changes), fa or self.fa])
        got = sorted((i["key"], i["severity"]) for i in r["issues"])
        self.assertEqual(got, sorted(expect), [i["message"] for i in r["issues"]])
        return r

    def test_01_price_math(self):
        self.check([("sales_price_math", "blocker")], sales_price_3c="501,000.00")

    def test_02_closing_not_a_date(self):
        self.check([("closing_date_invalid", "blocker")], closing_date="November 31")

    def test_03_closing_before_effective(self):
        self.check([("closing_before_effective", "blocker")], closing_date="October 1")

    def test_04_option_days_blank(self):
        self.check([("option_days_blank", "blocker")], option_days=" ")

    def test_05_header_address(self):
        r = self.check([("header_address", "blocker")], header_p4="125 Main St, Giddings, TX 78942")
        self.assertIn("Page 4 of 12", r["issues"][0]["message"])

    def test_06_initials_missing(self):
        r = self.check([("initials_seller", "blocker")], initials_p3_seller2=" ")
        self.assertIn("Page 3 of 12", r["issues"][0]["message"])

    def test_07_receipt(self):
        self.check([("receipt_mismatch", "blocker")], rcpt_option="250.00")

    def test_08_40_11_principal(self):
        self.check([("loan_amount_mismatch", "blocker")], fa=build_40_11("off", "150,000.00"))

    def test_12_address_blank_with_description(self):
        self.check([], address=" ", **{f"header_p{p}": " " for p in range(2, 13)})

    def test_13_no_description(self):
        self.check([("property_description_blank", "warning")], legal_desc_1=" ", address=" ", county=" ",
                   **{f"header_p{p}": " " for p in range(2, 13)})

    def test_rural_road_spelled_two_ways(self):
        addr = "4501 County Road 210, Giddings, TX 78942"
        self.check([], address=addr, **{f"header_p{p}": "4501 CR 210, Giddings, TX 78942" for p in range(2, 13)})

    def test_legal_description_headers_are_review_only(self):
        r = self.check([("header_address_differs", "warning")] * 11,
                       **{f"header_p{p}": "40 ac, Abstract 12, Lee County" for p in range(2, 13)})
        self.assertEqual(r["issues"][0]["message"],
                         'Page 2 of 12: the header reads "40 ac, Abstract 12, Lee County" and Paragraph 2A\'s address '
                         'is "123 Main St, Giddings, TX 78942". Confirm the header identifies this property.')

    def test_county_blank(self):
        self.check([("county", "blocker")], county=" ")

    def test_14_3d_will_and_will_not(self):
        self.check([("check_one_conflict", "blocker")], adj_will=True)

    def test_3d_will_without_target_or_days(self):
        self.check([("acreage_adjustment_review", "warning")] * 2, adj_will_not=False, adj_will=True, adj_acres="40",
                   adj_per_acre="5,000")

    def test_15_3d_will_acres_blank(self):
        self.check([("acreage_adjustment_incomplete", "blocker")], adj_will_not=False, adj_will=True,
                   adj_per_acre="5,000", adj_to_3a=True, adj_term_days="5")

    def test_16_3d_will_with_no_survey(self):
        self.check([("acreage_adjustment_no_survey", "warning")], adj_will_not=False, adj_will=True, adj_acres="40",
                   adj_per_acre="5,000", adj_to_3a=True, adj_term_days="5", survey_1=False, survey_1_days=" ",
                   survey_1_new_seller=False, survey_4=True)

    def test_3d_will_not_with_acres(self):
        self.check([("acreage_adjustment_conflict", "warning")], adj_acres="40")

    def test_17_title_expense_both(self):
        self.check([("check_one_conflict", "blocker")], title_exp_buyer=True)

    def test_18_survey_two_options(self):
        self.check([("check_one_conflict", "blocker")], survey_2=True)

    def test_19_survey_days_blank(self):
        self.check([("survey_days_blank", "blocker")], survey_1=False, survey_1_days=" ", survey_1_new_seller=False,
                   survey_2=True)

    def test_20_7h_is_and_is_not(self):
        self.check([("check_one_conflict", "blocker")], disc3_is=True)

    def test_21_7h_is_without_explanation(self):
        self.check([("disclosure_explain_blank", "warning")], disc1_not=False, disc1_is=True)

    def test_22_water_none(self):
        self.check([("water_disclosure_box_missing", "blocker")], water_1=False)

    def test_23_water_two(self):
        self.check([("check_one_conflict", "blocker")], water_3=True, water_provider_1="City of Giddings")

    def test_24_water_days_blank(self):
        self.check([("water_disclosure_days_blank", "blocker")], water_1=False, water_2=True)

    def test_26_residential_lease_not_listed(self):
        self.check([("lease_addendum_not_listed", "warning")], lease_4a_res=True)

    def test_27_reservation(self):
        # The spec expected special_provisions_business_term to fire too, but
        # the shared Paragraph 11 regex (tc_audit._BUSINESS_TERM_RE, unchanged)
        # has no match in this sentence: no $, %, "pay", "seller shall/will/to".
        self.check([("reservation_without_addendum", "warning")],
                   special_prov_1="Seller reserves 1/2 of the minerals")

    def test_28_propane_is_not_a_reservation(self):
        self.check([], special_prov_1="Propane gas tank conveys")

    def test_29_fee_agreement_with_12b(self):
        self.check([("fee_agreement_with_12b", "warning")], fee_a_seller=True, fee_a_cash_box=True, fee_a_cash="5,000",
                   b12_1_row=True, b12_1_dollar_box=True, b12_1_dollar="5,000")

    def test_30_7b_none(self):
        self.check([("disclosure_box_missing", "blocker")], sdn_1=False)

    def test_32_appraisal_without_tpf(self):
        # 3B financing 0, no 40-11: the appraisal review and nothing else.
        r = check_tc_file([build("p32", p22_appraisal=True, p22_tpf=False, tpf_3b_box=False, financing_3b="0",
                                 cash_3a="500,000.00")])
        self.assertEqual(keys(r), ["appraisal_without_tpf"])

    def test_7b2_days_blank(self):
        self.check([("disclosure_days_blank", "blocker")], sdn_1=False, sdn_2=True)

    def test_effective_date_blank(self):
        self.check([("effective_date", "blocker")], eff_month=" ")

    def test_10a_lease_not_listed(self):
        self.check([("possession_lease_not_listed", "warning")], poss_closing=False, poss_lease=True)

    def test_invalid_email(self):
        self.check([("email_invalid", "warning")], n_buyer_email="john.buyer@")


class WebRoute(unittest.TestCase):
    def test_upload_returns_25_17_report(self):
        import io
        import app as app_module
        with open(build("web", option_days=" "), "rb") as a, open(build_40_11("web"), "rb") as b:
            data = {"file": [(io.BytesIO(a.read()), "farm.pdf"), (io.BytesIO(b.read()), "40-11.pdf")],
                    "source_page": "claude_verify"}
        with mock.patch("app.maybe_send_unreadable_alert"), mock.patch("app.maybe_send_real_upload_alert"), \
                mock.patch("app.track_event"):
            r = app_module.app.test_client().post("/v1/tc/check", data=data, content_type="multipart/form-data",
                                                  headers={"User-Agent": "Mozilla/5.0 test"}).get_json()
        self.assertTrue(r["recognized"])
        self.assertEqual(r["form"], "TREC 25-17")
        self.assertEqual(r["files_line"], "TREC 25-17 + 40-11 addendum")
        self.assertEqual([i["message"] for i in r["display_issues"]],
                         ["Paragraph 5B: Option fee is entered but the option period (number of days) is blank"])


class Flattened(unittest.TestCase):
    def test_flattened_25_17_is_unreadable_not_all_blank(self):
        doc = fitz.open(build("flat"))
        doc.bake()
        path = os.path.join(os.path.dirname(build("tmp")), "flat_25-17.pdf")
        doc.save(path)
        r = check_tc_file([path])
        self.assertFalse(r["recognized"])
        self.assertTrue(r["unreadable"])
        self.assertEqual(keys(r), ["unrecognized"])
        self.assertIn("TREC 25-17", r["issues"][0]["message"])


class Routing(unittest.TestCase):
    def test_25_17_never_reaches_20_19_rules(self):
        trap = mock.Mock(side_effect=AssertionError("20-19 rules ran"))
        with mock.patch("tc_audit._consistency_issues", trap), mock.patch("tc_audit._contract_pages", trap):
            r = check_tc_file([build("routing"), build_40_11()])
        self.assertEqual(r["form"], "TREC 25-17")
        self.assertEqual(r["issues"], [])

    def test_outdated_25_16_is_not_checked(self):
        # TREC's fillable 25-16 (pdf-forms/25-16.pdf, sha256 705bbc8f31dceaa6...)
        # shares 324 field names with 25-17. Filled, it must run no field
        # rules: one outdated_form finding. Its Effective Date isn't
        # rect-verified, so it reads as blank -> the must-fix wording.
        doc = fitz.open(os.path.join(ROOT, "tests", "fixtures", "trec_25-16_blank.pdf"))
        for page in doc:
            for w in page.widgets():
                if w.field_type == fitz.PDF_WIDGET_TYPE_TEXT:
                    w.field_value = "200"
                    w.update()
        path = os.path.join(os.path.dirname(build("tmp")), "filled_25-16.pdf")
        doc.save(path)
        trap = mock.Mock(side_effect=AssertionError("field rules ran"))
        with mock.patch("trec_25_17._issues", trap), mock.patch("tc_audit._consistency_issues", trap):
            r = check_tc_file([path])
        self.assertEqual([(i["key"], i["severity"]) for i in r["issues"]], [("outdated_form", "blocker")])
        self.assertEqual(r["issues"][0]["message"], MUST_FIX_25_16)

    def _25_16(self, day, month, year):
        """TREC's fillable 25-16 with only its page-9 Effective Date filled
        (rects from txo-trec-qa's review: day, MONTH '20_2', year)."""
        doc = fitz.open(os.path.join(ROOT, "tests", "fixtures", "trec_25-16_blank.pdf"))
        want = dict(zip(("BROKER FILL IN THE DATE OF FINAL ACCEPTANCE", "20_2", "Effective Date"), (day, month, year)))
        for page in doc:
            for w in page.widgets():
                if w.field_name in want:
                    w.field_value = want[w.field_name]
                    w.update()
        path = os.path.join(os.path.dirname(build("tmp")), f"25-16_{month or 'blank'}.pdf")
        doc.save(path)
        return [(i["key"], i["severity"], i["message"]) for i in check_tc_file([path])["issues"]]

    def test_25_16_signed_before_mandatory_is_info(self):
        self.assertEqual(self._25_16("15", "May", "26"), [(
            "outdated_form", "info",
            "This contract is on TREC 25-16, and its Effective Date (May 15, 2026) is before TREC 25-17 became "
            "mandatory on 07/01/2026. TC Check doesn't check replaced editions, so nothing else was checked.")])

    def test_25_16_signed_after_mandatory_is_must_fix(self):
        self.assertEqual(self._25_16("20", "October", "26"), [("outdated_form", "blocker", MUST_FIX_25_16)])

    def test_25_16_blank_date_is_must_fix(self):
        self.assertEqual(self._25_16(" ", " ", " "), [("outdated_form", "blocker", MUST_FIX_25_16)])


if __name__ == "__main__":
    unittest.main()
