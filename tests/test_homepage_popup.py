"""Homepage Deal Notepad DEMO pop-up (2026-11-11): the public twin of
the member pop-up widget. It must render on the homepage for every
visitor, run the same in-page parser as the notepad section on the
sample note, offer the same download document, and — critically —
never save anything: the demo performs no network calls at all, so it
can never POST to the member /notepad endpoints.

    python3 -m unittest tests.test_homepage_popup
"""
import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
os.environ["ANALYTICS_PASSWORD"] = "test-internal"
# Throwaway Fernet key: some page renders touch filecrypt, which 500s on
# a malformed key. Test-only value, generated for this module.
os.environ["FILE_ENCRYPTION_KEY"] = "atRceqVlpntRmPCCJTL7zPNO0FWxsQCyOKQb599cv-0="
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import app as app_module  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) test"}
FIELDS = ("property", "buyers", "price", "earnest", "option", "closing",
          "financing", "credit", "inclusions", "survey")
FOOTER = ("Demo with a sample deal. Members save real deals to their "
          "dashboard, on the Agent plan.")


def _home_html():
    client = app_module.app.test_client()
    resp = client.get("/", headers=UA)
    assert resp.status_code == 200, resp.status_code
    return resp.get_data(as_text=True)


def _demo_segment(html):
    """The demo root markup: from the npd-root div up to the section
    that follows the notepad section on the homepage."""
    start = html.index('class="npd-root"')
    end = html.index("<!-- 2. WHAT IT CATCHES", start)
    return html[start:end]


class DemoMarkup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = _home_html()
        cls.seg = _demo_segment(cls.html)

    def test_pill_and_panel_markup(self):
        self.assertIn('id="npdPill"', self.seg)
        self.assertIn("&#9998; Notepad", self.seg)
        self.assertIn('id="npdPanel"', self.seg)
        self.assertIn("hidden", self.seg.split('id="npdPanel"')[1][:80])
        self.assertIn('id="npdClose"', self.seg)
        self.assertIn("Deal Notepad", self.seg)
        self.assertIn('class="npd-sample">Sample</span>', self.seg)

    def test_footer_line_and_plans_link(self):
        self.assertIn(FOOTER, self.seg)
        self.assertIn('<a href="#pricing">See the plans</a>', self.seg)

    def test_panel_textarea_prefilled_with_sample_note(self):
        start = self.seg.index('id="npdNotes"')
        area = self.seg[start:self.seg.index("</textarea>", start)]
        self.assertIn("Elm Creek Dr house, Austin.", area)
        self.assertIn("Buyers John &amp; Jane Doe.", area)
        self.assertIn("$685k.", area)
        self.assertIn("Earnest maybe 7k??", area)
        self.assertIn('id="npdRun"', self.seg)
        self.assertIn('id="npdDownload"', self.seg)
        self.assertIn("Download notes + Deal Sheet", self.seg)

    def test_panel_has_all_ten_deal_sheet_rows(self):
        for key in FIELDS:
            self.assertIn('data-np-row="%s"' % key, self.seg,
                          "panel row missing: %s" % key)


class DemoNeverSaves(unittest.TestCase):
    """The demo must not perform any network save. The member widget
    POSTs to the signed /notepad endpoints; the demo script (NOTEPAD_JS)
    is checked at the source level for the strongest form of that rule:
    it contains no fetch(, no XMLHttpRequest, and no sendBeacon at all,
    and never references a /notepad path — the only outbound call it can
    make is the pre-existing window.taEvt engagement ping on parse."""

    def test_notepad_js_has_no_network_primitives(self):
        js = app_module.NOTEPAD_JS
        self.assertNotIn("fetch(", js)
        self.assertNotIn("XMLHttpRequest", js)
        self.assertNotIn("sendBeacon", js)

    def test_notepad_js_never_references_member_notepad_endpoints(self):
        self.assertNotIn("/notepad", app_module.NOTEPAD_JS)


if __name__ == "__main__":
    unittest.main()
