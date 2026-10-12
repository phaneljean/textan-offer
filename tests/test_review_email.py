"""Review page "Email to Listing Agent" button (/review/<file> ->
/api/send-email -> integrations.send_offer_email).

Found 2026-10-12: the review page pasted the address into hand-quoted JS
strings, so an address with an apostrophe (123 O'Connor St) broke the whole
<script> and the button silently did nothing. These tests drive the real
SMS flow (Twilio mocked), check the page's JS actually parses, then send
with every outbound call mocked -- nothing reaches a real inbox.

    python3 -m unittest tests.test_review_email
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
# No real lookups or sends from a test run, whatever the shell has set.
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

AGENT = "+15125550100"


class ReviewEmailButton(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        from agent_profiles import save_agent_profile
        cls.app_module = app_module
        app_module.app.config["TESTING"] = True
        cls.client = app_module.app.test_client()
        # Complete profile + county in the text, so the required-fields gate
        # has nothing to block on and the button is enabled.
        save_agent_profile(AGENT, {
            "name": "Jane Agent", "license": "0123456", "phone": AGENT,
            "email": "jane@example.com", "brokerage": "Example Realty",
            "business_address": "123 Main St, Austin, TX 78701",
            "title_company": "Example Title", "title_company_address": "123 Main St, Austin, TX 78701",
        })

    def _text_offer(self, address):
        sent = []
        with mock.patch("app.twilio_send_sms", side_effect=lambda to, body: sent.append(body)):
            for body in (f"725k 3% 21day {address}", "YES"):
                self.assertEqual(self.client.post("/sms", data={"From": AGENT, "Body": body}).status_code, 200)
        m = re.search(r"http://localhost(/review/\S+)", "\n".join(sent))
        self.assertIsNotNone(m, f"no review link in SMS reply: {sent}")
        return m.group(1)

    def _assert_js_parses(self, html):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        for js in re.findall(r"<script>(.*?)</script>", html, re.S):
            path = os.path.join(_TMP, "review.js")
            with open(path, "w") as f:
                f.write(js)
            p = subprocess.run([node, "--check", path], capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)

    def test_apostrophe_address_keeps_button_working(self):
        path = self._text_offer("123 O'Connor St, Austin, TX Travis")
        html = self.client.get(path).get_data(as_text=True)
        self.assertIn('id="email-toggle">', html)  # enabled, not disabled
        self._assert_js_parses(html)
        self.assertNotIn("<h1>123 O'", html)  # HTML-escaped in markup too

        filename = path.split("/review/")[1].split("?")[0]
        q = dict(kv.split("=") for kv in path.split("?")[1].split("&"))
        with mock.patch("app.send_offer_email", return_value={"success": True}) as send:
            r = self.client.post("/api/send-email", json={
                "to_email": "listing@example.com", "pdf_filename": filename,
                "parsed": {"address": "123 O'Connor St", "price": 725000},
                "expires": q["expires"], "sig": q["sig"]})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(send.called)
        self.assertEqual(send.call_args[0][0], "listing@example.com")
        # Sent state persists: the page now offers a resend.
        self.assertIn("Resend to Listing Agent", self.client.get(path).get_data(as_text=True))

    def test_bad_signature_rejected(self):
        with mock.patch("app.send_offer_email") as send:
            r = self.client.post("/api/send-email", json={
                "to_email": "listing@example.com", "pdf_filename": "x.pdf", "expires": "1", "sig": "nope"})
        self.assertEqual(r.status_code, 401)
        self.assertFalse(send.called)


class SendOfferEmailUsesResend(unittest.TestCase):
    """Outbound moved to Resend 2026-10-07 (SendGrid has 0 credits). The
    offer email must go to Resend whenever RESEND_API_KEY is set."""

    def test_resend_with_pdf_attached(self):
        import integrations
        pdf = os.path.join(_TMP, "offer.pdf")
        with open(pdf, "wb") as f:
            f.write(b"%PDF-1.4 test")
        captured = []

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured.append(req)
            return _Resp()

        with mock.patch.object(integrations, "RESEND_API_KEY", "re_test"), \
                mock.patch.object(integrations, "SENDGRID_API_KEY", "SG.test"), \
                mock.patch("integrations.urllib.request.urlopen", side_effect=fake_urlopen):
            result = integrations.send_offer_email("listing@example.com", pdf,
                                                   {"address": "123 Main St", "price": 725000, "close_days": 21})
        self.assertEqual(result, {"success": True})
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].full_url, "https://api.resend.com/emails")
        import json
        body = json.loads(captured[0].data)
        self.assertEqual(body["to"], ["listing@example.com"])
        self.assertEqual(body["attachments"][0]["filename"], "offer.pdf")


if __name__ == "__main__":
    unittest.main()
