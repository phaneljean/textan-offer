"""Site changes from the 2026-10-13 design + conversion reviews: the hero's
illustrated report card must match what the real sample check returns, an
unreadable (flattened) PDF must not read as a title-blocking error or use a
free report, and /playground redirects to /demo.

    python3 -m unittest tests.test_site_polish
"""
import io
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
from tc_check_email import _display_issues, _clean

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) test"}
TX_SAMPLE = os.path.join(ROOT, "static", "sample_trec_20-19.pdf")
CO_SAMPLE = os.path.join(ROOT, "static", "sample_cbs1.pdf")


def _counts(path):
    shown = _display_issues(check_tc_file([path])["issues"])
    must = sum(1 for i in shown if i["severity"] == "blocker")
    return shown, must, len(shown) - must


def _unescape(html):
    # Curly vs. straight quotes is typography, not content -- fold both.
    return (html.replace("&rsquo;", "'").replace("&ldquo;", '"').replace("&rdquo;", '"')
                .replace("&sect;", "§").replace("&mdash;", "—"))


class HeroCardMatchesSample(unittest.TestCase):
    """The hero card is hand-written HTML; this is what keeps it honest when
    the sample PDFs or the rules change."""

    @classmethod
    def setUpClass(cls):
        cls.html = _unescape(app_module.app.test_client().get("/", headers=UA).get_data(as_text=True))
        m = re.search(r'id="heroReportCard".*?<div class="hx-caption">', cls.html, re.S)
        cls.card = m.group(0)

    def _state_block(self, state):
        return re.search(r'<div data-state="%s">(.*?)</ul>' % state, self.card, re.S).group(1)

    def test_tx_counts_and_findings(self):
        shown, must, review = _counts(TX_SAMPLE)
        block = self._state_block("tx")
        self.assertIn(f"{must} must fix", block)
        self.assertIn(f"{review} to review", block)
        self.assertIn(f"+ {len(shown) - 3} more in the full sample report", self.card)
        # Each finding on the card is a verbatim leading part of a real message
        real = [_clean(i["message"]).replace("\u2019", "'") for i in shown]
        for text in re.findall(r'<div class="hx-it"><b>[^<]*</b><span>([^<]*)</span>', block):
            self.assertTrue(any(r.startswith(text) for r in real), text)

    def test_co_counts_and_findings(self):
        shown, must, review = _counts(CO_SAMPLE)
        block = self._state_block("co")
        self.assertIn(f"{must} must fix", block)
        self.assertIn(f"{review} to review", block)
        real = [_clean(i["message"]).replace("\u2019", "'") for i in shown]
        texts = re.findall(r'<div class="hx-it"><b>[^<]*</b><span>([^<]*)</span>', block)
        self.assertEqual(len(texts), 3)
        for text in texts:
            self.assertTrue(any(r.startswith(text) for r in real), text)

    def test_hero_keeps_tracking_hooks(self):
        for hook in ('id="heroSampleBtn"', 'data-evt="hero_sample"', 'data-evt="hero_cta"', 'href="#check"',
                     'data-evt="state_toggle_tx"', 'data-evt="state_toggle_co"'):
            self.assertIn(hook, self.html)
        self.assertNotIn("home-hero.jpg", self.html)


class NavCta(unittest.TestCase):
    PAGES = ("/", "/demo", "/pricing", "/faq", "/about", "/trust", "/tc-hub", "/tc-hub/colorado",
             "/trec-changes", "/terms", "/privacy", "/contact", "/guides/trec-20-19-checklist")

    def test_every_nav_cta_is_short_and_nowrap(self):
        c = app_module.app.test_client()
        for path in self.PAGES:
            html = c.get(path, headers=UA).get_data(as_text=True)
            self.assertNotIn("Try TC Check Free", html, path)
            self.assertIn(">Check a file free</a>", html, path)
            self.assertIn('id="nav-cta-shared"', html, path)
            # injected after the page's own CSS so it wins the cascade
            self.assertLess(html.rfind("</style>", 0, html.find('id="nav-cta-shared"')), html.find('id="nav-cta-shared"'))


class ResultNextSteps(unittest.TestCase):
    def test_no_false_upsell_claims_and_own_file_cta_present(self):
        c = app_module.app.test_client()
        for path in ("/", "/tc-check"):
            html = c.get(path, headers=UA).get_data(as_text=True)
            for gone in ("never blank to begin with", "can never disagree", "nothing left for a check like this to catch",
                         "until every required TREC field", "buildUpsellCta(", "buildIndividualUpgradeCard("):
                self.assertNotIn(gone, html, (path, gone))
            self.assertIn("That was our sample. Now run one of yours.", html, path)
            self.assertIn("Check my own file &rarr;", html, path)
            self.assertIn("<strong>Next file?</strong> Forward it to", html, path)


def _flattened_sample():
    """The TX sample, flattened the way an e-sign export is: values baked
    into the page, form fields gone."""
    import fitz
    doc = fitz.open(TX_SAMPLE)
    doc.bake()
    return doc.tobytes()


class UnreadableFile(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        self.flat = _flattened_sample()

    def check(self, data, name="file.pdf", **form):
        form = {"file": (io.BytesIO(data), name), "source_page": "claude_verify", **form}
        return self.client.post("/v1/tc/check", data=form, headers=UA, content_type="multipart/form-data").get_json()

    def test_audit_result_is_not_a_blocker(self):
        path = os.path.join(_TMP, "flat.pdf")
        with open(path, "wb") as f:
            f.write(self.flat)
        r = check_tc_file([path])
        self.assertFalse(r["recognized"])
        self.assertTrue(r["unreadable"])
        self.assertEqual([i["severity"] for i in r["issues"]], ["info"])
        self.assertEqual(r["severity"]["level"], "unreadable")

    def test_api_no_blocker_and_no_free_report_used(self):
        r = self.check(self.flat)
        self.assertFalse(r["recognized"])
        self.assertEqual(r["blocker_count"], 0)
        self.assertFalse(r["gated"])
        self.assertTrue(r["free_report_not_used"])
        self.assertNotIn("free_reports_left", r)
        self.assertFalse(any(i["severity"] == "blocker" for i in r["display_issues"]))
        # Three more unreadable files still don't touch the allowance: the
        # next readable file is the first of the 3 free reports.
        for _ in range(3):
            self.check(self.flat)
        with open(TX_SAMPLE, "rb") as f:
            real = self.check(f.read(), name="sample.pdf")
        self.assertTrue(real["recognized"])
        self.assertEqual(real["free_reports_left"], app_module.TC_FREE_FULL_REPORTS - 1)
        self.assertFalse(real["gated"])

    def test_pages_word_it_honestly_and_by_state(self):
        for path in ("/", "/tc-check"):
            html = self.client.get(path, headers=UA).get_data(as_text=True)
            self.assertIn("We couldn&rsquo;t read the form fields in this file.", html, path)
            self.assertIn("stateMode() === 'co' ? 'CBS1' : 'TREC 20-19'", html, path)
            self.assertIn("This check didn&rsquo;t use one of your free reports.", html, path)
        self.assertNotIn("Not recognized as a TREC 20-19 or Colorado CBS1", self.client.get("/tc-check", headers=UA).get_data(as_text=True))


class PlaygroundAndSitemap(unittest.TestCase):
    def test_playground_301s_to_demo(self):
        r = app_module.app.test_client().get("/playground", headers=UA)
        self.assertEqual(r.status_code, 301)
        self.assertTrue(r.headers["Location"].endswith("/demo"))

    def test_sitemap_drops_playground_and_compare_only(self):
        xml = app_module.app.test_client().get("/sitemap.xml").get_data(as_text=True)
        self.assertNotIn("/playground<", xml)
        self.assertNotIn("/tc-check/compare<", xml)
        for kept in ("/tc-check<", "/demo" if "/demo<" in xml else "/pricing<", "/tc-check/bulk<", "/trust<"):
            self.assertIn(kept, xml)
        # compare page itself still works
        self.assertEqual(app_module.app.test_client().get("/tc-check/compare", headers=UA).status_code, 200)


if __name__ == "__main__":
    unittest.main()
