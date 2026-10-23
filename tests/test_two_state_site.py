"""Homepage and /tc-check serve Texas and Colorado from one page: both
states' copy is server-rendered (data-state="tx"/"co"), a class on <html>
picks which one shows (see _site_state in app.py), and the Colorado sample
check finds exactly what was planted in static/sample_cbs1.pdf.

    python3 -m unittest tests.test_two_state_site
"""
import os
import re
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import app as app_module
from tc_audit import check_tc_file

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) test"}


def html_class(html):
    return re.search(r'<html lang="en" class="([^"]*)">', html).group(1)


class TwoStatePages(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def get(self, path, **kw):
        return self.client.get(path, headers=UA, **kw).get_data(as_text=True)

    def test_both_variants_render_on_both_pages(self):
        for path in ("/", "/tc-check"):
            html = self.get(path)
            self.assertIn('data-state="tx"', html, path)
            self.assertIn('data-state="co"', html, path)
            self.assertIn('data-set-state="co"', html, path)
            self.assertIn('data-evt="state_toggle_co"', html, path)
            self.assertNotIn("__STATE_", html, path)
            self.assertNotIn("__TX_PRESSED__", html, path)
            # track_page_view still finds a bare <body> to tag
            self.assertIn('<body data-page="', html, path)

    def test_default_is_texas(self):
        for path in ("/", "/tc-check"):
            html = self.get(path)
            self.assertEqual(html_class(html), "state-tx", path)
            self.assertIn('data-set-state="tx" aria-pressed="true"', html)

    def test_state_param_cookie_and_src_pick_colorado(self):
        for path in ("/?state=co", "/tc-check?state=co", "/?src=co1012_test", "/?src=li_co_flyer"):
            self.assertEqual(html_class(self.get(path)), "state-co", path)
        c = app_module.app.test_client()
        c.set_cookie("ta_state", "co")
        self.assertEqual(html_class(c.get("/", headers=UA).get_data(as_text=True)), "state-co")
        # an explicit ?state=tx beats the cookie
        self.assertEqual(html_class(c.get("/?state=tx", headers=UA).get_data(as_text=True)), "state-tx")
        # a non-Colorado src is not a Colorado signal (fresh client: the
        # Colorado links above remembered themselves in a ta_state cookie)
        fresh = app_module.app.test_client()
        self.assertEqual(html_class(fresh.get("/?src=fb_trei", headers=UA).get_data(as_text=True)), "state-tx")
        self.assertIsNone(fresh.get_cookie("ta_state"))

    def test_colorado_section_and_kicker_badges_are_gone(self):
        html = self.get("/")
        self.assertNotIn('id="colorado"', html)
        self.assertNotIn("sl-hero-states", html)
        self.assertIn("Catch what title kicks back <em>before you send the file.</em>", html)

    def test_sms_disclosure_kept(self):
        html = self.get("/?state=co")
        # Copy updated 2026-10-23 (trec-qa Phase A, SMS nurture tips disclosed).
        self.assertIn("By texting, you agree to receive SMS replies and reminders about your offers, plus up to 2 tips "
                      "on the free trial. Reply STOP to opt out, HELP for help. Msg &amp; data rates may apply.", html)


class ColoradoSample(unittest.TestCase):
    def test_sample_cbs1_has_the_planted_findings(self):
        r = check_tc_file([os.path.join(ROOT, "static", "sample_cbs1.pdf")])
        self.assertEqual(r["form"], "Colorado CBS1")
        self.assertEqual(sorted(i["key"] for i in r["issues"]),
                         ["co_closing_holiday", "co_loan_avail_deleted", "co_price_math"])
        self.assertFalse(r["looks_like_blank_draft"])


if __name__ == "__main__":
    unittest.main()
