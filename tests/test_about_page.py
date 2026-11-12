"""Founder story placement (2026-11-11): the homepage founder section
is now a short MENTION — H2, one distilled paragraph, signature, and a
"Read the founder story" link — and the FULL story (the exact text the
homepage carried until 2026-11-11) lives on /about, which was reworked
from the old driveway-story page into the founder story page in the
site shell (_site_nav / _SITE_FOOTER). /about was already in
_SITEMAP_PATHS; these tests pin that it stays there.

    python3 -m unittest tests.test_about_page
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

# Sentences that exist ONLY in the full story (the homepage mention is
# distilled from the same copy but never carries these).
FULL_ONLY = (
    "The expensive mistakes were never in the big print.",
    "They were in a blank field on page seven, a date that does not "
    "exist, numbers that do not add up.",
    "TxtAnOffer reads a filled contract the same way, flags what title "
    "would flag, and deletes your file when it is done.",
)


def _get(path):
    client = app_module.app.test_client()
    resp = client.get(path, headers=UA)
    assert resp.status_code == 200, (path, resp.status_code)
    return resp.get_data(as_text=True)


class HomepageMention(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = _get("/")

    def test_mention_keeps_h2_photo_and_signature(self):
        self.assertIn("Built by someone who read<br>the fine print for "
                      "a living.", self.html)
        self.assertIn("/static/founder.jpg", self.html)
        self.assertIn("From the founder", self.html)

    def test_mention_paragraph_present(self):
        self.assertIn("Before TxtAnOffer, I reviewed group benefits "
                      "claims at Sun Life Canada, checking files against "
                      "policy wording line by line", self.html)
        self.assertIn("So I built the check I wished every file had "
                      "before it reached my desk.", self.html)

    def test_mention_links_to_about(self):
        self.assertIn('<a class="link" href="/about">Read the founder '
                      'story</a>', self.html)

    def test_full_story_no_longer_on_homepage(self):
        for sentence in FULL_ONLY:
            self.assertNotIn(sentence, self.html, sentence)


class AboutPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = _get("/about")

    def test_h1_is_the_founder_line(self):
        self.assertIn("<h1>Built by someone who read<br>the fine print "
                      "for a living.</h1>", self.html)

    def test_full_story_verbatim_sentences(self):
        self.assertIn("I am Phanel Jean-Baptiste. Before TxtAnOffer, I "
                      "reviewed group benefits claims at Sun Life Canada",
                      self.html)
        for sentence in FULL_ONLY:
            self.assertIn(sentence, self.html, sentence)

    def test_photo_and_signature(self):
        self.assertIn("/static/founder.jpg", self.html)
        self.assertIn("Phanel Jean-Baptiste", self.html)
        self.assertIn("Founder, TxtAnOffer", self.html)

    def test_old_about_story_gone(self):
        self.assertNotIn("Built After Listening to Texas Real Estate",
                         self.html)

    def test_company_block_and_links(self):
        self.assertIn("TxtAnOffer is a contract checker for Texas and "
                      "Colorado real estate:", self.html)
        self.assertIn('<a href="/tc-check" data-evt="about_check_cta">'
                      'TC Check</a>', self.html)
        self.assertIn('<a href="/#notepad">Deal Notepad</a>', self.html)

    def test_in_sitemap(self):
        sitemap = _get("/sitemap.xml")
        self.assertIn("<loc>https://txtanoffer.com/about</loc>", sitemap)
        self.assertIn("/about", app_module._SITEMAP_PATHS)


if __name__ == "__main__":
    unittest.main()
