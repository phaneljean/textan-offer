"""/tc-check's email gate (trec-qa, 2026-10-23): after the free full
reports, the gate's one number is the folded must-fix count -- the same
"N must fix" the report shows once unlocked (12 on the sample, not the raw
13 per-page blockers) -- with no category teaser, no shield badge and no
stats line.

    python3 -m unittest tests.test_tc_gate
"""
import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import app as app_module  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TX_SAMPLE = os.path.join(ROOT, "static", "sample_trec_20-19.pdf")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) test"}


class GateCountMatchesReport(unittest.TestCase):
    def _check(self, client, **form):
        with open(TX_SAMPLE, "rb") as f:
            data = {"file": (f, "sample.pdf"), "source_page": "claude_verify", **form}
            return client.post("/v1/tc/check", data=data, headers=UA, content_type="multipart/form-data").get_json()

    def test_gate_count_equals_report_must_fix(self):
        client = app_module.app.test_client()
        client.set_cookie("tc_cid", "gate-count-test")
        reports = [self._check(client) for _ in range(app_module.TC_FREE_FULL_REPORTS)]
        self.assertFalse(any(r["gated"] for r in reports))
        report_must_fix = sum(1 for i in reports[0]["display_issues"] if i["severity"] == "blocker")

        gated = self._check(client)
        self.assertTrue(gated["gated"])
        self.assertEqual(gated["issues"], [])
        self.assertEqual(gated["display_issues"], [])
        self.assertEqual(gated["must_fix_count"], report_must_fix)
        self.assertEqual(report_must_fix, 12)
        self.assertGreater(gated["blocker_count"], gated["must_fix_count"])  # raw count is per page
        for gone in ("categories", "social_proof"):
            self.assertNotIn(gone, gated)

    def test_page_renders_the_qa_gate(self):
        html = app_module.app.test_client().get("/tc-check", headers=UA).get_data(as_text=True)
        self.assertIn("data.must_fix_count", html)
        self.assertIn("' must-fix item' + (mustFix === 1 ? '' : 's') + ' found.'", html)
        self.assertIn(">See my full report</button>", html)
        for gone in ("Secure Audit", "will bounce at Title", "social_proof", "gate-shield", "See my bounce report"):
            self.assertNotIn(gone, html)
        self.assertFalse(app_module.PUBLIC_TC_STATS_ENABLED)


if __name__ == "__main__":
    unittest.main()
