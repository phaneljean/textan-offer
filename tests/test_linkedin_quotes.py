"""Said on LinkedIn section (2026-11-12): four verbatim comments from
outside people under TxtAnOffer LinkedIn posts, shown as cards directly
before the pricing section. No ratings, no review-platform wording.

    python3 -m unittest tests.test_linkedin_quotes
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

UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) test"}

# The four comments, verbatim (Albert's keeps its em dash).
QUOTES = (
    "The Effective Date field controls everything downstream. Miss it on "
    "page 1 and every option period, financing deadline, and closing date "
    "shifts before anyone catches it. One blank, entire timeline corrupted.",
    "Those tiny missing fields really do create outsized downstream work "
    "for TCs\u2014catching them before title seems like a meaningful "
    "time-saver.",
    "The revised description names the user, the documents and the problem "
    "in one pass. A sample flagged report could help people understand the "
    "output before they upload their own contract.",
    "Great tool for Texas brokers and transaction coordinators. Catching "
    "compliance issues before they become closing delays can save time, "
    "money, and unnecessary stress.",
)
NAMES = ("Mohit Tomar", "Albert van Zyl", "Yaroslav K.", "Jason Hammond")
ROLES = ("Co-Founder, VAConnect", "COO, Incode Group & LinkedRefs",
         "real estate funding")


def _get(path):
    return app_module.app.test_client().get(path, headers=UA).get_data(as_text=True)


def _unescape(html):
    return (html.replace("&rsquo;", "'").replace("&ldquo;", '"').replace("&rdquo;", '"')
                .replace("&sect;", "\u00a7").replace("&mdash;", "\u2014")
                .replace("&amp;", "&"))


class LinkedInQuotes(unittest.TestCase):
    def test_all_four_quotes_appear_verbatim(self):
        text = _unescape(_get("/"))
        for quote in QUOTES:
            self.assertIn('"%s"' % quote, text, quote[:40])

    def test_names_and_roles_appear(self):
        html = _unescape(_get("/"))
        for name in NAMES:
            self.assertIn(name, html, name)
        for role in ROLES:
            self.assertIn(role, html, role)

    def test_via_linkedin_label_on_every_card(self):
        html = _get("/")
        self.assertEqual(html.count("via LinkedIn"), 4)

    def test_section_sits_immediately_before_pricing(self):
        html = _get("/")
        quotes_at = html.index('<section class="quotes">')
        pricing_at = html.index('<section class="pricing-strip">')
        self.assertLess(quotes_at, pricing_at)
        between = html[quotes_at:pricing_at]
        self.assertEqual(between.count("<section"), 1,
                         "nothing else may sit between the quotes and pricing")

    def test_heading_is_honest_about_what_these_are(self):
        html = _get("/")
        self.assertIn("<h2>Said on LinkedIn.</h2>", html)
        self.assertIn("Comments under our posts.", html)

    def test_no_star_rating_or_review_platform_wording(self):
        # Scoped to the section itself: the rest of the page legitimately
        # contains "operatingSystem" (JSON-LD) and "rating" inside words.
        html = _get("/")
        section = html[html.index('<section class="quotes">'):
                       html.index('<section class="pricing-strip">')]
        self.assertNotIn("\u2605", section)
        self.assertIsNone(re.search(r"\bstars?\b", section, re.I))
        self.assertNotIn("rating", section.lower())
        self.assertNotIn("Reviews", section)

    def test_no_new_em_dash_in_section_copy(self):
        # The one em dash on the page lives inside Albert van Zyl's verbatim
        # quote; the section's own copy (heading, subhead, labels) has none.
        html = _get("/")
        section = _unescape(html[html.index('<section class="quotes">'):
                                 html.index('<section class="pricing-strip">')])
        self.assertEqual(section.count("\u2014"), 1,
                         "only the verbatim quote may carry an em dash")


if __name__ == "__main__":
    unittest.main()
