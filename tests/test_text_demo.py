"""Homepage "Text an offer" demo (text_demo.py, 2026-10-24): it must keep
showing the real SMS copy and real PDF crops, keep the SMS consent line, and
never leave a placeholder on the page.

    python3 -m unittest tests.test_text_demo
"""
import os
import re
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
os.environ["ANALYTICS_PASSWORD"] = "test-internal"
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import app as app_module  # noqa: E402
import text_demo  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _js_string(name):
    m = re.search(r'var %s = "((?:[^"\\]|\\.)*)";' % name, text_demo.JS)
    return m.group(1).encode().decode("unicode_escape")


class TextDemo(unittest.TestCase):
    def test_homepage_has_demo_and_no_placeholders(self):
        html = app_module.app.test_client().get("/").get_data(as_text=True)
        self.assertIn('id="textDemo"', html)
        self.assertIn("Get a filled draft back.", html)
        self.assertNotIn("__TEXT_DEMO", html)
        self.assertNotIn("hand_phone.jpg", html)

    def test_sms_consent_line_still_there(self):
        html = app_module.app.test_client().get("/").get_data(as_text=True)
        self.assertIn("By texting, you agree to receive SMS replies and reminders about your offers", html)
        self.assertIn('href="sms:+18338970333"', html)

    def test_images_exist(self):
        for src in re.findall(r'src="/static/(text_demo/[^"]+)"', text_demo.HTML):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, "static", src)), src)

    def test_confirmation_bubble_matches_real_sms(self):
        built = app_module.build_offer_draft(_js_string("OFFER"), "+15125550199")
        parsed = built[0] if isinstance(built, tuple) else built
        real = app_module.format_offer_confirmation(parsed)
        shown = _js_string("REPLY1")
        # Same text, except the closing line, which the real reply computes from today.
        norm = lambda s: re.sub(r"Closing [A-Z][a-z]{2}, [A-Z][a-z]{2} \d{1,2}, \d{4} \(21 days[^)]*\)", "Closing DATE", s)
        self.assertTrue(norm(real).startswith(norm(shown)), "demo bubble no longer matches format_offer_confirmation()")
        # ...and the shown line is what drafting it on Sat Oct 24, 2026 really gives.
        self.assertIn(app_module.closing_line(21, "2026-10-24T17:00:00"), shown)

    def test_ready_bubble_matches_real_sms(self):
        src = open(os.path.join(ROOT, "app.py")).read()
        self.assertIn('f"Your TREC draft for {draft[\'address\']} is ready.\\n\\n"', src)
        self.assertIn('f"Includes {includes}. Draft only: review every page before signing.\\n\\n"', src)


if __name__ == "__main__":
    unittest.main()
