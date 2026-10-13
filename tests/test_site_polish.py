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


if __name__ == "__main__":
    unittest.main()
