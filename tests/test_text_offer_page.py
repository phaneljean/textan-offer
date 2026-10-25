"""/text-an-offer (text_offer_page.py, 2026-10-24): the homepage's Text an
offer section links to it, it renders, and its worked examples and claims
still match what the parser, draft builder and PDF filler really do.

    python3 -m unittest tests.test_text_offer_page
"""
import os
import re
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
os.environ["ANALYTICS_PASSWORD"] = "test-internal"
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import app as app_module  # noqa: E402
import text_offer_page  # noqa: E402
from parser import parse_offer_sms  # noqa: E402
from subscriptions import FREE_OFFER_LIMIT  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Macintosh) test"}


class TextOfferPage(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_route_renders(self):
        r = self.client.get("/text-an-offer", headers=UA)
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("What to put in the text", html)
        self.assertIsNone(re.search(r"__[A-Z_]+__", html))  # no unfilled placeholder
        self.assertIn('href="sms:+18338970333"', html)
        self.assertIn('href="/pricing"', html)
        self.assertIn('href="/privacy"', html)
        self.assertIn('href="/terms"', html)
        self.assertIn('<nav class="nav">', html)

    def test_homepage_learn_more_link(self):
        html = self.client.get("/", headers=UA).get_data(as_text=True)
        section = html.split('<section class="text" id="text">', 1)[1].split("</section>", 1)[0]
        self.assertIn('<a class="btn ghost" href="/text-an-offer" data-evt="text_learn_more">Learn more</a>', section)
        self.assertIn("text_learn_more", app_module._ENGAGEMENT_TYPES)

    def test_in_sitemap(self):
        self.assertIn("<loc>https://txtanoffer.com/text-an-offer</loc>",
                      self.client.get("/sitemap.xml").get_data(as_text=True))

    def test_consent_line_matches_homepage(self):
        home = self.client.get("/", headers=UA).get_data(as_text=True)
        self.assertIn(text_offer_page.SMS_CONSENT, home)

    def test_only_example_address_is_123_main_st(self):
        html = self.client.get("/text-an-offer", headers=UA).get_data(as_text=True)
        body = html.split("<footer", 1)[0]
        streets = set(re.findall(r"\b\d{2,5} [A-Z][a-z]+ (?:St|Ave|Dr|Ln|Rd|Blvd)\b", body))
        self.assertEqual(streets, {"123 Main St"})

    def test_examples_match_real_drafts(self):
        a, b, c = [t for t, _ in text_offer_page.EXAMPLES]
        p, err, w = app_module.build_offer_draft(a, "+15125550199")
        self.assertIsNone(err)
        self.assertFalse(p["_tx_needs_confirm"])
        self.assertEqual((p["address"], p["city"]), ("123 Main St", "Austin"))
        self.assertEqual((p["down_payment_amount"], p["loan_amount"], p["price"]), (21750, 703250, 725000))
        self.assertEqual((p["earnest_money"], p["option_fee"]), (7250, 250))
        self.assertEqual(p["close_days"], 21)
        self.assertEqual(p["county"], "")  # "not set by this text"

        p, err, w = app_module.build_offer_draft(b, "+15125550199")
        self.assertIsNone(err)
        self.assertEqual((p["down_payment_amount"], p["loan_amount"]), (725000, 0))

        p, err, w = app_module.build_offer_draft(c, "+15125550199")
        self.assertIsNone(err)
        self.assertEqual((p["down_payment_amount"], p["loan_amount"], p["price"]), (240000, 960000, 1200000))
        self.assertEqual((p["financing_type"], p["inspection_days"], p["has_hoa"], p["close_days"]), ("fha", 10, True, 45))
        self.assertEqual((p["city"], p["county"]), ("Dallas", ""))  # the city isn't read as the county

    def test_summary_shown_matches_real_confirmation(self):
        p, _, _ = app_module.build_offer_draft(text_offer_page.EXAMPLES[0][0], "+15125550199")
        real = app_module.format_offer_confirmation(p)
        shown = re.search(r'<div class="to-reply">(.*?)</div>', text_offer_page.body(), re.S).group(1)
        # The shown closing line is the real one for a text sent Sat Oct 24, 2026
        # (as the caption under it says); today's differs only in that line.
        example = app_module.closing_line(21, "2026-10-24T17:00:00")
        self.assertEqual(example, "Closing Mon, Nov 16, 2026 (21 days, moved off the weekend)")
        self.assertIn(example + "\n", shown)
        self.assertIn("Example sent Sat, Oct 24, 2026: 21 days out is a Saturday, so closing moves to Monday.",
                      text_offer_page.body())
        real = real.replace(app_module.closing_line(21), example)
        self.assertTrue(real.startswith(shown), "summary on the page no longer matches format_offer_confirmation()")

    def test_shorthands_on_page_parse(self):
        # Every price / down / closing form the page teaches.
        for t, field, want in [
            ("725k 3% 21day 123 Main St, Austin, TX", "price", 725000),
            ("725,000 3% 21day 123 Main St, Austin, TX", "price", 725000),
            ("725000 3% 21day 123 Main St, Austin, TX", "price", 725000),
            ("1.2m 3% 21day 123 Main St, Austin, TX", "price", 1200000),
            ("725k 3 percent 21day 123 Main St, Austin, TX", "down_payment_pct", 0.03),
            ("725k 3 pct 21day 123 Main St, Austin, TX", "down_payment_pct", 0.03),
            ("725k 3 down 21day 123 Main St, Austin, TX", "down_payment_pct", 0.03),
            ("725k cash 21day 123 Main St, Austin, TX", "down_payment_pct", 1.0),
            ("725k 3% 21 days 123 Main St, Austin, TX", "close_days", 21),
            ("725k 3% 21day 10 day inspection 123 Main St, Austin, TX", "inspection_days", 10),
            ("725k 3% 21day Travis 123 Main St, Austin, TX", "county", "Travis"),
            ("725k 3% 21day Travis County 123 Main St, Austin, TX", "county", "Travis"),
            ("725k 3% 21day 123 Denton Dr, Austin, TX", "county", None),
            ("725k 3% 21day conventional 123 Main St, Austin, TX", "financing_type", "conventional"),
            ("725k 3% 21day FHA 123 Main St, Austin, TX", "financing_type", "fha"),
            ("725k 3% 21day VA 123 Main St, Austin, TX", "financing_type", "va"),
            ("725k 3% 21day USDA 123 Main St, Austin, TX", "financing_type", "usda"),
            ("725k 3% 21day Texas Veterans 123 Main St, Austin, TX", "financing_type", "texas_veterans"),
            ("725k 3% 21day reverse mortgage 123 Main St, Austin, TX", "financing_type", "reverse_mortgage"),
        ]:
            r = parse_offer_sms(t)
            self.assertEqual(r.get(field), want, t)
            self.assertEqual(r["address"], "123 Denton Dr" if "Denton" in t else "123 Main St", t)
        self.assertIn("close_days", parse_offer_sms("725k 3% close Dec 15 123 Main St, Austin, TX"))

    def test_missing_and_out_of_range_replies(self):
        self.assertIn("Couldn't find a down payment %. Try something like 3%.",
                      parse_offer_sms("725k 21day 123 Main St, Austin, TX")["error"])
        self.assertIn("error", parse_offer_sms("725k 3% 5day 123 Main St, Austin, TX"))
        self.assertIn("error", parse_offer_sms("725k 3% 400day 123 Main St, Austin, TX"))
        self.assertIn("error", parse_offer_sms("725k 60% 21day 123 Main St, Austin, TX"))
        self.assertIn("error", parse_offer_sms("51m 3% 21day 123 Main St, Austin, TX"))

    def test_state_claims(self):
        # No state -> must resend; other state -> no contract; CO -> CBS1.
        p, err, w = app_module.build_offer_draft("725k 3% 21day 123 Main St", "+15125550199")
        self.assertTrue(p["_tx_needs_confirm"])
        _, err, _ = app_module.build_offer_draft("725k 3% 21day 123 Main St, Phoenix, AZ", "+15125550199")
        self.assertIn("WAITLIST", err)
        p, err, _ = app_module.build_offer_draft("725k 3% 21day 123 Main St, Denver, CO 80205", "+15125550199")
        self.assertIsNone(err)
        self.assertEqual(p["state"], "CO")

    def test_price_claims(self):
        self.assertEqual(FREE_OFFER_LIMIT, 3)
        pricing = self.client.get("/pricing", headers=UA).get_data(as_text=True)
        self.assertIn("$40", pricing)
        self.assertIn("First 3 offers free", pricing)

    def test_no_hoa_form_number(self):
        # TREC may have replaced 36-10; the page says "the HOA addendum".
        html = self.client.get("/text-an-offer", headers=UA).get_data(as_text=True)
        self.assertNotIn("36-10", html)
        self.assertNotIn("36&#8209;10", html)

    def test_forms_list_matches_finalize(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")) as fh:
            src = fh.read()
        for f in ('"40-11 Financing Addendum"', '"36-10 HOA Addendum"', '["61-0 Water Disclosure", "IABS"]'):
            self.assertIn(f, src)


if __name__ == "__main__":
    unittest.main()
