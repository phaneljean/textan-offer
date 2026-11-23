"""Homepage Deal Notepad demo — REMOVED from the homepage 2026-11-22
at Baptiste's direction. The section (interactive demo, app window,
desk block with the desktop downloads and the PWA Install button) no
longer renders on "/". The notepad product itself stays live in the
member dashboard (/notepad); the /pricing page still names it. These
tests assert the homepage removal so the section does not creep back,
and keep the NOTEPAD_JS purity checks: the demo script remains in
app.py (unused by the homepage now) and must still contain no network
primitives and no reference to the member /notepad endpoints.

History: the section shipped 2026-11-11; the floating pop-up demo and
the section download button were removed the same day; the whole
section was removed 2026-11-22.

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


class SectionRemoved(unittest.TestCase):
    """The homepage carries no notepad section, card, or demo."""

    @classmethod
    def setUpClass(cls):
        cls.html = _home_html()

    def test_no_notepad_section_markup(self):
        for marker in ('<section class="notepad"', 'id="notepad"',
                       "<!-- 4a2. NOTEPAD:", 'id="npNotes"', 'id="npRun"',
                       'id="npRows"', 'id="npWorkflow"', 'id="npDraft"',
                       'class="np-app"', "Take the notepad to your desk.",
                       "Your deal notes,", "home_tools_notepad"):
            self.assertNotIn(marker, self.html, marker)

    def test_no_desktop_downloads_on_homepage(self):
        self.assertNotIn("TxtAnOffer.Notepad_0.1.1_x64-setup.exe", self.html)
        self.assertNotIn("TxtAnOffer.Notepad_0.1.1_aarch64.dmg", self.html)
        self.assertNotIn('data-evt="desk_download_windows"', self.html)
        self.assertNotIn('data-evt="desk_download_mac"', self.html)

    def test_no_popup_markup_on_homepage(self):
        for marker in ('id="npdPill"', 'id="npdPanel"', 'id="npdClose"',
                       'id="npdNotes"', 'id="npdRun"', 'id="npdRows"',
                       'id="npdNext"', 'id="npdDownload"',
                       'class="npd-root"', "npd-"):
            self.assertNotIn(marker, self.html, marker)

    def test_no_popup_footer_copy(self):
        self.assertNotIn(POPUP_FOOTER, self.html)

    def test_desk_strip_is_two_tools(self):
        self.assertIn("One desk, two tools", self.html)
        self.assertIn("<h3>TC Check</h3>", self.html)
        self.assertIn("<h3>Text an Offer</h3>", self.html)
        self.assertNotIn("<h3>Deal Notepad</h3>", self.html)


class DemoScriptPurity(unittest.TestCase):
    """NOTEPAD_JS stays in app.py (the member product's demo logic is
    untouched); it must still perform no network calls and never
    reference the member /notepad endpoints."""

    def test_notepad_js_has_no_network_primitives(self):
        js = app_module.NOTEPAD_JS
        self.assertNotIn("fetch(", js)
        self.assertNotIn("XMLHttpRequest", js)
        self.assertNotIn("sendBeacon", js)

    def test_notepad_js_never_references_member_notepad_endpoints(self):
        self.assertNotIn("/notepad", app_module.NOTEPAD_JS)

    def test_notepad_js_has_no_popup_or_download_leftovers(self):
        js = app_module.NOTEPAD_JS
        self.assertNotIn("npd", js)
        self.assertNotIn("npDownload", js)
        self.assertNotIn("downloadText", js)


if __name__ == "__main__":
    unittest.main()
