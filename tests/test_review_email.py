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
import time
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
# No real lookups or sends from a test run, whatever the shell has set.
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

class ReviewEmailButton(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        from agent_profiles import save_agent_profile
        cls.app_module = app_module
        app_module.app.config["TESTING"] = True
        cls.client = app_module.app.test_client()
        cls._phones = iter(range(100, 200))
        cls._last_offer = 0.0

    def _text_offer(self, address):
        """Texts one offer from a fresh agent (own phone, so no test ever hits
        the 3-offer free limit) with a complete profile + county in the text,
        so the required-fields gate has nothing to block on. Returns the
        review path and the agent's phone."""
        from agent_profiles import save_agent_profile
        phone = f"+1512555{next(self._phones):04d}"
        save_agent_profile(phone, {
            "name": "Jane Agent", "license": "0123456", "phone": phone,
            "email": "jane@example.com", "brokerage": "Example Realty",
            "business_address": "123 Main St, Austin, TX 78701",
            "title_company": "Example Title", "title_company_address": "123 Main St, Austin, TX 78701",
        })
        # Offer filenames are street + a 1-second timestamp, so two offers for
        # the same street in the same second share a file. Wait out the second.
        time.sleep(max(0.0, 1.05 - (time.time() - ReviewEmailButton._last_offer)))
        sent = []
        with mock.patch("app.twilio_send_sms", side_effect=lambda to, body: sent.append(body)):
            for body in (f"725k 3% 21day {address}", "YES"):
                self.assertEqual(self.client.post("/sms", data={"From": phone, "Body": body}).status_code, 200)
        m = re.search(r"http://localhost(/review/\S+)", "\n".join(sent))
        self.assertIsNotNone(m, f"no review link in SMS reply: {sent}")
        ReviewEmailButton._last_offer = time.time()
        return m.group(1), phone

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
        path, phone = self._text_offer("123 O'Connor St, Austin, TX Travis")
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

    def test_webhook_button_only_sets_its_own_offers_webhook(self):
        path, phone = self._text_offer("123 Main St, Austin, TX Travis")
        filename = path.split("/review/")[1].split("?")[0]
        q = dict(kv.split("=") for kv in path.split("?")[1].split("&"))
        body = {"url": "https://example.com/hook", "filename": filename, "expires": q["expires"], "sig": q["sig"]}
        with mock.patch("app.has_professional_access", return_value=True), \
                mock.patch("app._is_safe_webhook_url", return_value=True), \
                mock.patch("app.save_webhook") as save:
            other = self.client.post("/api/webhook", json=dict(body, source_id="+15125559999"))
            self.assertEqual(other.status_code, 403)
            self.assertFalse(save.called)
            own = self.client.post("/api/webhook", json=dict(body, source_id=phone))
            self.assertEqual(own.status_code, 200, own.get_json())
            save.assert_called_once_with(phone, "https://example.com/hook")

    def test_first_offer_profile_tip_link_opens(self):
        # Fresh agent with no profile: the first-offer SMS tip must link to a
        # profile page that actually opens, not the bare /profile 403.
        time.sleep(max(0.0, 1.05 - (time.time() - ReviewEmailButton._last_offer)))
        sent = []
        with mock.patch("app.twilio_send_sms", side_effect=lambda to, body: sent.append(body)):
            for body in ("725k 3% 21day 123 Main St, Austin, TX", "YES"):
                self.client.post("/sms", data={"From": "+15125559111", "Body": body})
        ReviewEmailButton._last_offer = time.time()
        m = re.search(r"Tip: .*? at http://localhost(/profile\S*)", "\n".join(sent))
        self.assertIsNotNone(m, sent)
        self.assertEqual(self.client.get(m.group(1)).status_code, 200)

    def test_transaction_add_task_handler_parses(self):
        # The Transaction workspace (dashboard -> "Transaction" on an
        # accepted offer) used onsubmit="return addTask(event, "Title")":
        # the inner quotes ended the attribute, so "Add a task" never ran.
        from html.parser import HTMLParser
        from offers_db import record_thread_response
        path, phone = self._text_offer("123 Main St, Austin, TX Travis")
        filename = path.split("/review/")[1].split("?")[0]
        record_thread_response(filename, "accept")
        tx_path = self.app_module.sign_transaction_url(filename)
        with mock.patch("app.has_professional_access", return_value=True):
            html = self.client.get(tx_path).get_data(as_text=True)
            handlers = []

            class _P(HTMLParser):
                def handle_starttag(self, tag, attrs):
                    if tag == "form" and dict(attrs).get("onsubmit"):
                        handlers.append(dict(attrs)["onsubmit"])
            _P().feed(html)
            self.assertTrue(handlers)
            node = shutil.which("node")
            if node:
                js = os.path.join(_TMP, "handlers.js")
                with open(js, "w") as f:
                    f.write("".join(f"function h{i}(event){{{h}}}\n" for i, h in enumerate(handlers)))
                p = subprocess.run([node, "--check", js], capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stderr)
            r = self.client.post(tx_path, json={"action": "add", "stage": "Title", "label": "Order title commitment"})
            self.assertEqual(r.status_code, 200, r.get_json())
            self.assertIn("Order title commitment", [t["label"] for t in r.get_json()["tasks"]])

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
        # Footer invite to TC Check (2026-10-25): link with its src tag
        # exactly once, below the offer content. No HTML part is sent.
        link = "https://txtanoffer.com/tc-check?src=offer_email_listing"
        self.assertEqual(body["text"].count(link), 1)
        self.assertEqual(body["text"].count("tc-check"), 1)
        self.assertLess(body["text"].index("Price: $725,000"), body["text"].index(link))
        self.assertNotIn("html", body)
        self.assertEqual(body["subject"], "Purchase Offer - 123 Main St")

    def test_sendgrid_fallback_has_same_footer(self):
        import integrations
        import app as app_module
        pdf = os.path.join(_TMP, "offer_sg.pdf")
        with open(pdf, "wb") as f:
            f.write(b"%PDF-1.4 test")
        captured = []

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(integrations, "RESEND_API_KEY", ""), \
                mock.patch.object(integrations, "SENDGRID_API_KEY", "SG.test"), \
                mock.patch("integrations.urllib.request.urlopen",
                           side_effect=lambda req, timeout=None: captured.append(req) or _Resp()):
            integrations.send_offer_email("listing@example.com", pdf,
                                          {"address": "123 Main St", "price": 725000, "close_days": 21})
        import json
        content = json.loads(captured[0].data)["content"]
        self.assertEqual([c["type"] for c in content], ["text/plain"])
        self.assertEqual(content[0]["value"].count("https://txtanoffer.com/tc-check?src=offer_email_listing"), 1)
        # The footer's "3 free reports, no signup" is only true while this holds.
        self.assertIn("3 free reports, no signup", integrations.OFFER_EMAIL_FOOTER)
        self.assertEqual(app_module.TC_FREE_FULL_REPORTS, 3)


if __name__ == "__main__":
    unittest.main()
