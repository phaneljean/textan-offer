"""Draft auto-check (2026-11-11): finalize_offer_sms runs TC Check on the
just-generated draft and adds one terse line to the agent's reply SMS.
The line builder must never raise and must stay silent ("") whenever the
check cannot speak for the file, so a checker failure can never block or
delay the draft link.

    python3 -m unittest tests.test_draft_auto_check
"""
import os
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
os.environ["ANALYTICS_PASSWORD"] = "test-internal"
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import app as app_module  # noqa: E402


def _line(audit):
    with mock.patch.object(app_module, "check_tc_file", return_value=audit):
        return app_module._draft_check_line("/tmp/whatever.pdf")


class DraftCheckLine(unittest.TestCase):
    def test_clean_draft(self):
        self.assertEqual(
            _line({"recognized": True, "complete": True, "issues": []}),
            "TC Check on this draft: nothing flagged on the fields it verifies.")

    def test_blockers_and_review(self):
        issues = [
            {"severity": "blocker", "message": "x", "key": "sales_price_math"},
            {"severity": "blocker", "message": "x", "key": "earnest_money_amount"},
            {"severity": "warning", "message": "x", "key": "county"},
        ]
        self.assertEqual(
            _line({"recognized": True, "complete": False, "issues": issues}),
            "TC Check on this draft: 2 must-fix: Price Math, Earnest Money; 1 to review.")

    def test_blocker_labels_capped_at_three(self):
        issues = [{"severity": "blocker", "message": "x", "key": k} for k in
                  ("address", "city", "county", "buyer_name", "seller_name")]
        line = _line({"recognized": True, "complete": False, "issues": issues})
        self.assertIn("5 must-fix: Address, City, County +2 more.", line)

    def test_review_only(self):
        issues = [{"severity": "warning", "message": "x", "key": "county"}]
        self.assertEqual(
            _line({"recognized": True, "complete": False, "issues": issues}),
            "TC Check on this draft: 1 to review.")

    def test_unrecognized_is_silent(self):
        self.assertEqual(_line({"recognized": False, "issues": []}), "")
        self.assertEqual(_line(None), "")
        self.assertEqual(_line({}), "")

    def test_checker_exception_is_silent(self):
        with mock.patch.object(app_module, "check_tc_file",
                               side_effect=RuntimeError("bad pdf")):
            self.assertEqual(app_module._draft_check_line("/tmp/x.pdf"), "")

    def test_line_is_gsm7_safe(self):
        issues = [{"severity": "blocker", "message": "x", "key": "sales_price_math"}]
        line = _line({"recognized": True, "complete": False, "issues": issues})
        line.encode("ascii")  # raises if any non-ASCII slipped in


if __name__ == "__main__":
    unittest.main()
