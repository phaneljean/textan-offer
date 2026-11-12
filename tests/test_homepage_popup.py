"""Homepage Deal Notepad demo (2026-11-11; pop-up + download removed
2026-11-11): the homepage section runs the in-page parser on the
sample note and shows the product's workflow (action items,
reminders, draft + TC Check) computed client-side from that parse.

The floating pop-up demo (npdPill / npdPanel) and the section's
"Download notes + Deal Sheet" button were removed at Baptiste's
direction — the downloadable desktop app replaces the pop-up
concept, and these tests now assert their absence so neither creeps
back. The demo still performs no network calls at all, so it can
never POST to the member /notepad endpoints.

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
POPUP_FOOTER = ("Demo with a sample deal. Members save real deals to their "
                "dashboard, on the Agent plan.")


def _home_html():
    client = app_module.app.test_client()
    resp = client.get("/", headers=UA)
    assert resp.status_code == 200, resp.status_code
    return resp.get_data(as_text=True)


def _section_segment(html):
    """The notepad section markup: from its comment banner up to the
    WHAT IT CATCHES section that follows it (the pop-up demo root used
    to sit between the two and is gone)."""
    start = html.index("<!-- 4a2. NOTEPAD:")
    end = html.index("<!-- 2. WHAT IT CATCHES", start)
    return html[start:end]


class DemoRemoved(unittest.TestCase):
    """The pop-up demo and the homepage download buttons are gone."""

    @classmethod
    def setUpClass(cls):
        cls.html = _home_html()
        cls.sec = _section_segment(cls.html)
        cls.js = app_module.NOTEPAD_JS

    def test_no_popup_markup_on_homepage(self):
        for marker in ('id="npdPill"', 'id="npdPanel"', 'id="npdClose"',
                       'id="npdNotes"', 'id="npdRun"', 'id="npdRows"',
                       'id="npdNext"', 'id="npdDownload"',
                       'class="npd-root"', "npd-"):
            self.assertNotIn(marker, self.html, marker)

    def test_no_popup_footer_copy(self):
        self.assertNotIn(POPUP_FOOTER, self.html)

    def test_no_section_download_button(self):
        self.assertNotIn('id="npDownload"', self.sec)
        self.assertNotIn("Download notes + Deal Sheet", self.html)
        self.assertNotIn("np-dl", self.sec)

    def test_notepad_js_has_no_popup_or_download_leftovers(self):
        self.assertNotIn("npd", self.js)
        self.assertNotIn("npDownload", self.js)
        self.assertNotIn("downloadText", self.js)
        self.assertNotIn("docText", self.js)
        self.assertNotIn("buildDoc", self.js)
        self.assertNotIn("Blob", self.js)

    def test_section_still_offers_the_demo(self):
        # What stays: the editable sample note, Clean it up, the rows.
        self.assertIn('id="npNotes"', self.sec)
        self.assertIn("Elm Creek Dr house, Austin.", self.sec)
        self.assertIn('id="npRun"', self.sec)
        self.assertIn("Clean it up", self.sec)
        self.assertIn('id="npRows"', self.sec)
        for key in ("property", "buyers", "price", "earnest", "option",
                    "closing", "financing", "credit", "inclusions",
                    "survey"):
            self.assertIn('data-np-row="%s"' % key, self.sec,
                          "section row missing: %s" % key)


class WorkflowMarkup(unittest.TestCase):
    """The homepage demo shows the product's workflow (action items,
    reminders, draft + TC Check), populated client-side from the same
    parse. The containers render empty/hidden in the static HTML; the
    generators live in NOTEPAD_JS."""

    @classmethod
    def setUpClass(cls):
        cls.html = _home_html()
        cls.sec = _section_segment(cls.html)
        cls.js = app_module.NOTEPAD_JS

    def test_section_workflow_containers(self):
        self.assertIn('id="npWorkflow" hidden', self.sec)
        self.assertIn('id="npActions"', self.sec)
        self.assertIn('id="npReminders"', self.sec)
        self.assertIn("Counted from the contract once it is signed.", self.sec)
        self.assertIn('id="npDraft"', self.sec)
        self.assertIn("Create draft", self.sec)
        self.assertIn('id="npDraftOut" hidden', self.sec)
        self.assertIn('id="npDraftText"', self.sec)
        self.assertIn('id="npDraftReply"', self.sec)
        self.assertIn("You text to (833) 897-0333", self.sec)
        self.assertIn("Sample flow. In the product, the draft is a real "
                      "filled PDF and the check runs on it.", self.sec)

    def test_workflow_generators_in_js(self):
        for name in ("buildActions", "buildReminders", "buildDraftText",
                     "buildDraftReply", "closingLine", "renderWorkflow",
                     "fillList", "showDraft"):
            self.assertIn("function " + name, self.js, name)
        # The honest strings the generators must be able to produce.
        self.assertIn("Confirm ", self.js)
        self.assertIn("Deliver earnest money to the escrow agent", self.js)
        self.assertIn("Collect the HOA documents", self.js)
        self.assertIn("Earnest money due", self.js)
        self.assertIn("Option period ends", self.js)
        self.assertIn("No closing date in your notes yet", self.js)
        self.assertIn("TC Check on this draft: 2 must-fix: Price Math, "
                      "Earnest Money; 1 to review.", self.js)
        self.assertIn("Draft ready: TREC 20-19 for ", self.js)


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
