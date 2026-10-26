"""Form detection (2026-10-25): TC Check reads the printed TREC form number
and edition stamp before it runs any 20-19 check. A 9-18 or 25-17 is
recognized but not checked yet (they matched the 20-19 field names and got
a fake 20-19 report before this), an outdated form gets one finding, and a
current 20-19 report is byte-for-byte what it was.

Fixtures are TREC's own blanks, downloaded 2026-10-25 from the PDF each
form's trec.texas.gov page links:
  trec_25-17_blank.pdf  pdf-forms/25-17_4.pdf  sha256 91f945fe6e4067a5...
  trec_20-18_blank.pdf  pdf-forms/20-18.pdf    sha256 52fa502f0824a55e...
  trec_40-10_blank.pdf  pdf-forms/40-10.pdf    sha256 66fc4539b507384e...
tc_check_golden_20-19.json is check_tc_file() output captured on 7e56cf9
(before the detector existed).

    python3 -m unittest tests.test_form_detect
"""
import io
import json
import os
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_PATH", os.path.join(_TMP, "test.db"))
os.environ.setdefault("OFFER_OUTPUT_DIR", os.path.join(_TMP, "offers"))
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)

import trec_forms  # noqa: E402
import tc_check_email  # noqa: E402
import real_upload_alert  # noqa: E402
from tc_audit import check_tc_file, compare_contracts  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")
SAMPLE = os.path.join(ROOT, "static", "sample_trec_20-19.pdf")


def fx(name):
    return os.path.join(FIX, name)


def text_only(form_id, edition):
    """A one-page PDF with just a TREC footer stamp. 9-18 work is paused
    (Phanel, 2026-10-25), so no 9-18 fixture; detection is text-only anyway."""
    import fitz
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), f"TREC NO. {form_id}   Page 1 of 11   {edition}")
    path = os.path.join(_TMP, f"text_only_{form_id}.pdf")
    doc.save(path)
    return path


class Detection(unittest.TestCase):
    def test_each_fixture(self):
        cases = {
            SAMPLE: ("20-19", "05-04-2026"),
            fx("trec_20-19_planted_errors.pdf"): ("20-19", "05-04-2026"),
            fx("trec_25-17_blank.pdf"): ("25-17", "05-04-2026"),
            fx("trec_20-18_blank.pdf"): ("20-18", "11-04-2024"),
            fx("trec_40-10_blank.pdf"): ("40-10", "11-07-2022"),
            fx("trec_40-11_clean.pdf"): ("40-11", "11-04-2024"),
            fx("trec_41-3_clean.pdf"): ("41-3", "11-07-2022"),
            fx("CBS1_blank_2026.pdf"): (None, None),
        }
        for path, want in cases.items():
            with self.subTest(os.path.basename(path)):
                self.assertEqual(trec_forms.detect_form(path), want)

    def test_replaces_line_is_not_the_form(self):
        # Every 20-19 page footer says "This form replaces TREC NO. 20-18."
        self.assertEqual([f for f, _ in trec_forms.detect_forms(SAMPLE)], ["20-19"])

    def test_status(self):
        cases = {("20-19", "05-04-2026"): "current", ("20-18", None): "outdated", ("20-17", None): "outdated",
                 ("9-16", None): "outdated", ("9-17", None): "outdated", ("25-16", None): "outdated",
                 ("23-19", None): "outdated", ("24-19", None): "outdated", ("30-17", None): "outdated",
                 ("40-10", None): "outdated", ("11-8", None): "outdated", ("36-10", None): "outdated",
                 ("20-19", "11-04-2024"): "outdated", ("20-20", None): "newer", ("62-0", None): "current",
                 ("99-1", None): "unknown"}
        for (fid, ed), want in cases.items():
            with self.subTest(fid=fid, ed=ed):
                self.assertEqual(trec_forms.status(fid, ed), want)
        self.assertEqual(trec_forms.current_for("11-8"), "11-9")


class NotChecked(unittest.TestCase):
    def _only(self, result, key):
        self.assertFalse(result["recognized"])
        self.assertFalse(result["unreadable"])
        self.assertTrue(result["unsupported"])
        self.assertEqual(len(result["issues"]), 1, result["issues"])  # no fake 20-19 findings
        self.assertEqual(result["issues"][0]["key"], key)
        return result["issues"][0]

    def test_9_18_recognized_nothing_checked(self):
        r = check_tc_file([text_only("9-18", "05-04-2026")])
        issue = self._only(r, "form_not_supported")
        self.assertEqual(issue["message"], "TREC 9-18 Unimproved Property Contract (05-04-2026) recognized -- "
                                           "checks for this form are coming; nothing was checked.")
        self.assertEqual(issue["severity"], "info")
        self.assertEqual((r["form"], r["form_id"], r["edition"]), ("TREC 9-18", "9-18", "05-04-2026"))

    def test_25_17_goes_to_its_own_rules(self):
        # Since the 25-17 rules landed (trec_25_17.py); tests/test_25_17.py has them.
        r = check_tc_file([fx("trec_25-17_blank.pdf")])
        self.assertEqual(r["form"], "TREC 25-17")
        self.assertNotIn("city", [i["key"] for i in r["issues"]])

    def test_outdated_20_18_one_finding(self):
        # Before 2026-10-25 this blank 20-18 got 13 20-19 findings.
        issue = self._only(check_tc_file([fx("trec_20-18_blank.pdf")]), "outdated_form")
        # txo-trec-qa's wording (spec_9-18_25-17.md section 1). Old editions'
        # Effective Date isn't rect-verified, so it's read as blank: must-fix.
        self.assertEqual(issue["message"], "This file is on TREC 20-18 (11-04-2024). TREC 20-19 (05-04-2026) is the "
                                           "current One to Four Family Residential Contract (Resale), mandatory since "
                                           "07/01/2026. TC Check reads 20-19 only. Redo the contract on 20-19.")
        self.assertEqual(issue["severity"], "blocker")

    def test_signed_before_mandatory_wording(self):
        self.assertEqual(trec_forms.outdated_contract_message("25-16", "11-04-2024", signed_before_mandatory=True),
                         "This contract is on TREC 25-16, which was current when it was signed. TC Check reads "
                         "only the current 25-17, so no other checks were run.")

    def test_newer_edition(self):
        import fitz
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "TREC NO. 9-19   Page 1 of 11   05-04-2027")
        path = os.path.join(_TMP, "fake_9-19.pdf")
        doc.save(path)
        issue = self._only(check_tc_file([path]), "form_not_supported")
        self.assertEqual(issue["message"], "This is a newer TREC edition (9-19) than TC Check knows; no checks were run.")
        self.assertEqual(issue["severity"], "info")

    def test_addendum_alone(self):
        issue = self._only(check_tc_file([fx("trec_40-11_clean.pdf")]), "form_not_supported")
        self.assertIn("together with the TREC 20-19", issue["message"])
        self._only(check_tc_file([fx("trec_40-10_blank.pdf")]), "outdated_form")

    def test_registry_plugs_in(self):
        fake = {"recognized": True, "issues": []}
        with mock.patch.dict("tc_audit.FORM_CHECKERS", {"9-18": lambda paths, det, pages: fake}):
            self.assertIs(check_tc_file([text_only("9-18", "05-04-2026")]), fake)

    def test_compare_refuses_other_forms(self):
        r = compare_contracts(fx("trec_20-18_blank.pdf"), fx("trec_20-19_planted_errors.pdf"))
        self.assertFalse(r["recognized"])
        self.assertIn("TREC 20-18", r["message"])


def _filled(src):
    """Every text field filled, so the old field-count routing (3 matched
    20-19 names) would call it a 20-19 -- what a real filled 9-18 did."""
    import fitz
    doc = fitz.open(src)
    for page in doc:
        for w in page.widgets():
            if w.field_type == fitz.PDF_WIDGET_TYPE_TEXT:
                w.field_value = "200"
                w.update()
    out = os.path.join(_TMP, "filled_" + os.path.basename(src))
    doc.save(out)
    return out


class Routing(unittest.TestCase):
    """txo-trec-qa spec 0.1: a filled 9-18 / 25-17 (or an old 20-18) must
    never reach the 20-19 rules. Before the detector all three did."""

    def test_new_forms_never_reach_20_19_rules(self):
        trap = mock.Mock(side_effect=AssertionError("20-19 rules ran"))
        for name in ("trec_25-17_blank.pdf", "trec_20-18_blank.pdf"):
            path = _filled(fx(name))
            with self.subTest(name), mock.patch("tc_audit._consistency_issues", trap), \
                    mock.patch("tc_audit._contract_pages", trap):
                r = check_tc_file([path])
                self.assertNotIn("city", [i["key"] for i in r["issues"]])   # 20-19-only rule
                if name == "trec_20-18_blank.pdf":
                    self.assertEqual([i["key"] for i in r["issues"]], ["outdated_form"])
                else:
                    self.assertEqual(r["form"], "TREC 25-17")

    def test_20_19_still_routes_to_20_19_rules(self):
        r = check_tc_file([fx("trec_20-19_clean_two_parties.pdf")])
        self.assertTrue(r["recognized"])
        self.assertNotIn("unsupported", r)


class TwentyNineteenUnchanged(unittest.TestCase):
    def test_golden_output_identical(self):
        with open(fx("tc_check_golden_20-19.json")) as f:
            golden = json.load(f)
        cases = json.loads(golden.pop("_cases"))
        for name, paths in cases.items():
            with self.subTest(name):
                got = json.dumps(check_tc_file([os.path.join(ROOT, p) for p in paths]))
                self.assertEqual(got, golden[name])

    def test_outdated_attachment_adds_one_finding(self):
        base = check_tc_file([fx("trec_20-19_planted_errors.pdf")])
        r = check_tc_file([fx("trec_20-19_planted_errors.pdf"), fx("trec_40-10_blank.pdf")])
        self.assertTrue(r["recognized"])
        outdated = [i for i in r["issues"] if i["key"] == "outdated_form"]
        self.assertEqual(len(outdated), 1)
        self.assertEqual(outdated[0]["message"], "This is an outdated TREC form (40-10, 11-07-2022); the current "
                                                 "form is TREC 40-11 Third Party Financing Addendum (11-04-2024).")
        rest = [i for i in r["issues"] if i["key"] not in ("outdated_form", "extra_file_unrecognized")]
        self.assertEqual(rest, base["issues"])


class Channels(unittest.TestCase):
    def setUp(self):
        self.r = check_tc_file([text_only("9-18", "05-04-2026")])

    def test_email_says_nothing_was_checked(self):
        body = tc_check_email.format_reply_body(self.r)
        self.assertIn("nothing was checked", body)
        self.assertNotIn("couldn't read", body.lower())
        html = tc_check_email.format_reply_html(self.r)
        self.assertIn("Nothing was checked", html)
        self.assertEqual(tc_check_email.subject_line(self.r), "TC File Check: TREC 9-18 recognized, not checked")

    def test_no_hand_review_reply_to(self):
        with mock.patch.dict(os.environ, {"HAND_REVIEW_EMAIL": "hand@example.com"}):
            self.assertIsNone(tc_check_email.reply_to_for(self.r))
            self.assertNotIn("by hand", tc_check_email.format_reply_body(self.r))

    def test_alert_wording(self):
        subject, body = real_upload_alert.format_not_checked_alert(self.r, "web upload (tc_check_page)")
        self.assertIn("TREC 9-18 recognized, not checked", subject)
        self.assertNotIn("couldn't be read", body)

    def test_web_route(self):
        import app as app_module
        with mock.patch("app.maybe_send_unreadable_alert"), mock.patch("app.maybe_send_real_upload_alert"), \
                mock.patch("app.track_event"), mock.patch.dict(os.environ, {"HAND_REVIEW_EMAIL": "hand@example.com"}):
            with open(text_only("9-18", "05-04-2026"), "rb") as f:
                data = {"file": (io.BytesIO(f.read()), "9-18.pdf"), "source_page": "claude_verify"}
            r = app_module.app.test_client().post("/v1/tc/check", data=data, content_type="multipart/form-data",
                                                  headers={"User-Agent": "Mozilla/5.0 test"}).get_json()
        self.assertTrue(r["unsupported"])
        self.assertFalse(r["recognized"])
        self.assertFalse(r["hand_review"])
        self.assertEqual(r["display_issues"][0]["message"], "TREC 9-18 Unimproved Property Contract (05-04-2026) "
                                                            "recognized — checks for this form are coming; nothing was checked.")

    def test_upload_page_claims_no_new_forms(self):
        import app as app_module
        html = app_module.app.test_client().get("/tc-check", headers={"User-Agent": "Mozilla/5.0 test"}).get_data(as_text=True)
        import re
        html = re.sub(r"<script.*?</script>", "", html, flags=re.S)  # visible copy, not code comments
        for claim in ("9-18", "25-17", "Farm and Ranch", "Unimproved"):
            self.assertNotIn(claim, html)


if __name__ == "__main__":
    unittest.main()
