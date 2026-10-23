"""Calendly "Book a free 15-min file check" link (2026-10-14). Replaces the
mailto "Schedule a call" / "Walk me through it" buttons, adds one line under
the drop zone on / and /tc-check and one line in TC Check report emails.
These keep every placement tagged (utm_content for Calendly, data-evt for
/analytics), opening in a new tab, and counted on the /analytics strip.

    python3 -m unittest tests.test_booking_link
"""
import os
import re
import sqlite3
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import analytics
import app as app_module
import tc_check_email

UA = {"User-Agent": "Mozilla/5.0 (Macintosh) test"}
CAL = "https://calendly.com/pejeanbaptiste/15-min-file-check?utm_source=txtanoffer&amp;utm_content="


def _book_links(html):
    return re.findall(r'<a[^>]*href="https://calendly\.com/[^"]*"[^>]*>', html)


class BookingLinkPlacements(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        c = app_module.app.test_client()
        cls.html = {p: c.get(p, headers=UA).get_data(as_text=True)
                    for p in ("/", "/?state=co", "/tc-check", "/tc-check?state=co", "/brokers", "/tc-hub", "/tc-hub/colorado")}

    def _assert_links(self, path, placements, evt):
        links = _book_links(self.html[path])
        self.assertEqual(len(links), len(placements), links)
        for tag, placement in zip(links, placements):
            with self.subTest(path=path, placement=placement):
                self.assertIn(CAL + placement + '"', tag)
                self.assertIn('target="_blank"', tag)
                self.assertIn('rel="noopener"', tag)
                self.assertIn(f'data-evt="{evt}"', tag)

    def test_homepage(self):
        # The simplified homepage (2026-10-23, Phanel-approved mockup) has no
        # booking link; /tc-check, /brokers and the hubs keep theirs. The
        # book_call_home name stays in BOOK_CALL_EVENTS for past rows.
        for path in ("/", "/?state=co"):
            self._assert_links(path, [], "book_call_home")
            self.assertNotIn("Schedule a call", self.html[path])
        self.assertIn("book_call_home", app_module.BOOK_CALL_EVENTS)

    def test_tc_check_both_states(self):
        for path in ("/tc-check", "/tc-check?state=co"):
            self._assert_links(path, ["tc_check"], "book_call_tccheck")

    def test_brokers(self):
        self._assert_links("/brokers", ["brokers_audit"], "book_call_brokers")
        self.assertNotIn("Walk me through it", self.html["/brokers"])
        # Plain contact links stay.
        self.assertIn('href="mailto:support@txtanoffer.com"', self.html["/brokers"])

    def test_hubs(self):
        self._assert_links("/tc-hub", ["tc_hub"], "book_call_hub")
        self._assert_links("/tc-hub/colorado", ["co_hub"], "book_call_hub")

    def test_no_advice_wording(self):
        for path, html in self.html.items():
            for bad in ("review your contract for you", "contract advice", "legal advice call"):
                self.assertNotIn(bad, html.lower(), path)


class BookingLinkInReportEmail(unittest.TestCase):
    RESULT = {"recognized": True, "form": "20-19", "state": "TX",
              "property": {"address": "123 Main St", "city": "Austin", "county": "Travis"},
              "issues": [{"severity": "blocker", "field": "effective_date", "message": "Effective Date is blank."}]}

    def test_text_and_html(self):
        text = tc_check_email.format_reply_body(self.RESULT)
        html = tc_check_email.format_reply_html(self.RESULT)
        url = "https://calendly.com/pejeanbaptiste/15-min-file-check?utm_source=txtanoffer&utm_content=report_email"
        self.assertIn("Want to go over these findings? Book a free 15-min call: " + url, text)
        self.assertIn('href="' + url.replace("&", "&amp;") + '" target="_blank" rel="noopener"', html)
        self.assertIn("Want to go over these findings?", html)
        # Near the end, but the disclaimer/footer still closes the email.
        self.assertLess(text.index(url), text.index("---\n"))
        self.assertLess(html.index("15-min-file-check"), html.index("isn&#x27;t legal advice"))

    def test_clean_report_wording(self):
        clean = dict(self.RESULT, issues=[])
        self.assertIn("Want to walk through this report? Book a free 15-min call:", tc_check_email.format_reply_body(clean))

    def test_footer_keeps_mailing_address(self):
        old = tc_check_email.MAILING_ADDRESS
        tc_check_email.MAILING_ADDRESS = "TxtAnOffer · 807 W 112th St, Los Angeles, CA 90044"
        try:
            text = tc_check_email.format_reply_body(self.RESULT)
            html = tc_check_email.format_reply_html(self.RESULT)
        finally:
            tc_check_email.MAILING_ADDRESS = old
        self.assertTrue(text.rstrip().endswith("807 W 112th St, Los Angeles, CA 90044"))
        self.assertLess(html.index("15-min-file-check"), html.index("807 W 112th St"))

    def test_unrecognized_reply_has_no_booking_line(self):
        self.assertNotIn("calendly", tc_check_email.format_reply_body({"recognized": False}))


class BookingClicksCounted(unittest.TestCase):
    def setUp(self):
        conn = sqlite3.connect(analytics.DB_PATH)
        conn.execute("DELETE FROM events")
        conn.commit()
        conn.close()
        self.client = app_module.app.test_client()

    def _beacon(self, etype, page, visitor):
        self.client.set_cookie("ta_vid", visitor)
        return self.client.post("/v1/evt", data={"type": etype, "page": page}, headers=UA)

    def test_whitelisted(self):
        for name in ("book_call_home", "book_call_tccheck", "book_call_brokers", "book_call_hub"):
            self.assertIn(name, app_module._ENGAGEMENT_TYPES)
            self.assertIn(name, analytics.BOOK_CALL_EVENTS)

    def test_headline_and_analytics_page(self):
        self._beacon("book_call_home", "homepage", "a" * 32)
        self._beacon("book_call_tccheck", "tc_check_page", "a" * 32)    # same browser: 1
        self._beacon("book_call_hub", "tc_hub_page", "b" * 32)
        self._beacon("book_call_brokers", "brokers_page", "c" * 32)
        h = analytics.get_headline(days=7)
        self.assertEqual(h["clicked_book_call"], 3)
        self.assertEqual(h["clicked_check"], 0)       # a booking click isn't a check click
        old = app_module.ANALYTICS_PASSWORD
        app_module.ANALYTICS_PASSWORD = "pw"
        try:
            html = self.client.get("/analytics?token=pw", headers=UA).get_data(as_text=True)
        finally:
            app_module.ANALYTICS_PASSWORD = old
        self.assertRegex(html, r'<div class="v">3</div><div class="k">Clicked book a call</div>')


if __name__ == "__main__":
    unittest.main()
