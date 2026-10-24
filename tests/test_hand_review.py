"""S1 (2026-10-24): a file TC Check can't read (flattened Dotloop/Authentisign
export, scan) ends in "Send it, we'll check it by hand." instead of a dead
end -- on the web and in the tc@check reply -- but only when
HAND_REVIEW_EMAIL is set; Phanel gets an alert either way; and the wording
makes no reply-time promise and never says the file was checked.

    python3 -m unittest tests.test_hand_review
"""
import base64
import io
import json
import os
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
os.environ["TC_CHECK_EMAIL_TOKEN"] = "test-token"
os.environ["ANALYTICS_PASSWORD"] = "test-internal"
os.environ.pop("HAND_REVIEW_EMAIL", None)
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import app as app_module  # noqa: E402
import integrations  # noqa: E402
import real_upload_alert  # noqa: E402
import tc_check_email  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TX_SAMPLE = os.path.join(ROOT, "static", "sample_trec_20-19.pdf")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) test"}
HAND = "handreview@example.com"
OUTSIDER = "jane@brokerage-example.com"
LINE = "Send it, we'll check it by hand."
# Things the copy must never say (RULES rule 1, D4: no single-file turnaround).
NEVER = ("business day", "within", "24 hours", "guarantee", "we checked", "was checked by hand",
         "calendly", "$349", "20-file audit")


def _flattened():
    import fitz
    doc = fitz.open(TX_SAMPLE)
    doc.bake()
    return doc.tobytes()


FLAT = _flattened()
with open(TX_SAMPLE, "rb") as _f:
    READABLE = _f.read()


class _InlineThread:
    def __init__(self, target, daemon=None):
        self.target = target

    def start(self):
        self.target()


@mock.patch("real_upload_alert.threading.Thread", _InlineThread)
@mock.patch("app.threading.Thread", _InlineThread)
@mock.patch("real_upload_alert.send_plain_email")
@mock.patch("app.send_html_email", return_value={"success": True})
class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._p = [mock.patch("app.check_and_increment", return_value=True),
                  mock.patch("app.TC_CHECK_EMAIL_TOKEN", "test-token"),
                  mock.patch("app.ANALYTICS_PASSWORD", "test-internal")]
        for p in cls._p:
            p.start()

    @classmethod
    def tearDownClass(cls):
        for p in cls._p:
            p.stop()

    def _web(self, data=FLAT, cookie=None, **form):
        c = app_module.app.test_client()
        if cookie:
            c.set_cookie(*cookie)
        c.set_cookie("ta_src", "sp1023_a1")
        body = {"file": (io.BytesIO(data), "contract.pdf"), "source_page": "tc_check_page", **form}
        r = c.post("/v1/tc/check", data=body, headers=UA, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def _forward(self, sender=OUTSIDER, data=FLAT):
        body = {"from": f"Jane <{sender}>", "SPF": "pass", "attachments": "1",
                "attachment1": (io.BytesIO(data), "contract.pdf")}
        r = app_module.app.test_client().post("/v1/tc/check/email/test-token", data=body,
                                              content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)

    def _unreadable_alerts(self, alert):
        return [c for c in alert.call_args_list if c[0][1].startswith("Unreadable TC Check file")]

    # --- off by default -------------------------------------------------
    def test_unset_keeps_old_reply_and_no_button(self, send, alert):
        with mock.patch.dict(os.environ, {"HAND_REVIEW_EMAIL": ""}):
            r = self._web()
            self.assertFalse(r["hand_review"])
            self._forward()
        to, subject, text, html = send.call_args[0]
        self.assertEqual(to, OUTSIDER)
        self.assertIsNone(send.call_args[1]["reply_to"])
        self.assertNotIn(LINE, text)
        self.assertIn("re-sending the original fillable PDF", text)
        # Phanel is still alerted, and told no hand check was offered.
        a = self._unreadable_alerts(alert)
        self.assertEqual(len(a), 2)
        self.assertIn("HAND_REVIEW_EMAIL isn't set", a[-1][0][2])

    # --- on ---------------------------------------------------------------
    def test_web_unreadable_offers_hand_check(self, send, alert):
        with mock.patch.dict(os.environ, {"HAND_REVIEW_EMAIL": HAND}):
            r = self._web()
            demo = self._web(is_demo="1")
            readable = self._web(READABLE)
        self.assertTrue(r["hand_review"])
        self.assertTrue(r["free_report_not_used"])  # still no tc_use recorded
        self.assertEqual(r["blocker_count"], 0)
        self.assertFalse(demo["hand_review"])
        self.assertFalse(readable["hand_review"])

    def test_email_forward_reply_and_reply_to(self, send, alert):
        with mock.patch.dict(os.environ, {"HAND_REVIEW_EMAIL": HAND}):
            self._forward()
        to, subject, text, html = send.call_args[0]
        self.assertEqual(to, OUTSIDER)
        self.assertEqual(send.call_args[1]["reply_to"], HAND)
        self.assertTrue(text.startswith(LINE))
        self.assertIn("Send it, we&rsquo;ll check it by hand.", html)
        self.assertIn("Reply to this email with the file attached.", text)
        self.assertIn("isn't deleted automatically", text)
        # trec-qa 2026-10-24 edits, word for word.
        for s in ("E-signed copies are often flattened, and scans have no form fields, so TC Check can't read them automatically.",
                  "A hand check looks for the same kinds of blanks and mismatches TC Check flags. It isn't legal advice."):
            self.assertIn(s, text)
            self.assertIn(s.replace("'", "&rsquo;"), html)
        for bad in NEVER:
            self.assertNotIn(bad, text.lower(), bad)
            self.assertNotIn(bad, html.lower(), bad)
        a = self._unreadable_alerts(alert)
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0][0][0], "support@txtanoffer.com")
        self.assertIn(f"Sender: {OUTSIDER}", a[0][0][2])
        self.assertIn("They were offered a hand check", a[0][0][2])

    def test_web_report_email_gets_reply_to(self, send, alert):
        with mock.patch.dict(os.environ, {"HAND_REVIEW_EMAIL": HAND}):
            self._web(email=OUTSIDER)
        self.assertEqual(send.call_args[1]["reply_to"], HAND)
        self.assertTrue(send.call_args[0][2].startswith(LINE))

    def test_readable_forward_unchanged(self, send, alert):
        with mock.patch.dict(os.environ, {"HAND_REVIEW_EMAIL": HAND}):
            self._forward(data=READABLE)
        self.assertIsNone(send.call_args[1]["reply_to"])
        self.assertNotIn(LINE, send.call_args[0][2])
        self.assertEqual(self._unreadable_alerts(alert), [])

    # --- alert exclusions ---------------------------------------------------
    def test_alert_skips_demo_verify_internal_and_phanel(self, send, alert):
        with mock.patch.dict(os.environ, {"HAND_REVIEW_EMAIL": HAND}):
            self._web(is_demo="1")
            self._web(source_page="claude_verify")
            self._web(cookie=("ta_internal", "test-internal"))
            self._forward(sender="pejeanbaptiste@gmail.com")
        self.assertEqual(self._unreadable_alerts(alert), [])
        with mock.patch.dict(os.environ, {"HAND_REVIEW_EMAIL": HAND}):
            self._web()
        a = self._unreadable_alerts(alert)
        self.assertEqual(len(a), 1)
        self.assertIn("src tag: sp1023_a1", a[0][0][2])
        self.assertIn("unknown (no email given)", a[0][0][2])

    # --- the web "send it" endpoint ----------------------------------------
    def _send_it(self, email=OUTSIDER, files=None, **form):
        files = files if files is not None else [(io.BytesIO(FLAT), "Smith - 123 Main St.pdf")]
        body = {"file": files, "email": email, "source_page": "tc_check_page", **form}
        return app_module.app.test_client().post("/v1/tc/check/hand-review", data=body, headers=UA,
                                                 content_type="multipart/form-data")

    def test_send_it_unset_is_503(self, send, alert):
        with mock.patch.dict(os.environ, {"HAND_REVIEW_EMAIL": ""}):
            r = self._send_it()
        self.assertEqual(r.status_code, 503)
        send.assert_not_called()

    def test_send_it_emails_file_to_hand_review_inbox(self, send, alert):
        with mock.patch("app.track_event") as track, mock.patch.dict(os.environ, {"HAND_REVIEW_EMAIL": HAND}):
            r = self._send_it()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"ok": True})
        to, subject, text, html = send.call_args[0]
        self.assertEqual(to, HAND)
        self.assertEqual(send.call_args[1]["reply_to"], OUTSIDER)
        att = send.call_args[1]["attachments"]
        self.assertEqual(len(att), 1)
        self.assertEqual(att[0]["filename"], "Smith - 123 Main St.pdf")
        self.assertEqual(base64.b64decode(att[0]["content"]), FLAT)
        self.assertIn(OUTSIDER, subject)
        self.assertIn("isn't deleted automatically", text)
        self.assertEqual(track.call_args[0][0], "tc_hand_review_requested")

    def test_send_it_rejects_bad_input_and_skips_verify(self, send, alert):
        with mock.patch.dict(os.environ, {"HAND_REVIEW_EMAIL": HAND}):
            self.assertEqual(self._send_it(email="nope").status_code, 400)
            self.assertEqual(self._send_it(files=[(io.BytesIO(b"x"), "photo.jpg")]).status_code, 400)
            self.assertEqual(self._send_it(files=[]).status_code, 400)
            v = self._send_it(source_page="claude_verify")
        self.assertEqual(v.get_json(), {"ok": True, "verify": True})
        send.assert_not_called()

    def test_send_it_failure_is_not_confirmed(self, send, alert):
        send.return_value = {"success": False, "error": "x"}
        with mock.patch.dict(os.environ, {"HAND_REVIEW_EMAIL": HAND}):
            r = self._send_it()
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("ok", r.get_json())


class PageCopy(unittest.TestCase):
    def test_page_has_line_and_no_promises(self):
        html = app_module.app.test_client().get("/tc-check", headers=UA).get_data(as_text=True)
        start = html.index("function buildHandReview")
        block = html[start:html.index("function renderResult", start)]
        self.assertIn("Send it, we&rsquo;ll check it by hand.", block)
        self.assertIn("isn&rsquo;t deleted automatically", block)
        self.assertIn("Leave your email and we&rsquo;ll send this file (and any addenda you added) to Phanel at TxtAnOffer, "
                      "who will check it by hand and write back. A hand check looks for the same kinds of blanks and "
                      "mismatches TC Check flags. It isn&rsquo;t legal advice. A file you send this way is emailed to our "
                      "inbox and stays there; it isn&rsquo;t deleted automatically the way TC Check&rsquo;s own check is.", block)
        self.assertIn("if (data.hand_review) return buildHandReview", html)
        for bad in NEVER:
            self.assertNotIn(bad, block.lower(), bad)


class SendersPassReplyTo(unittest.TestCase):
    def test_resend_payload(self):
        seen = {}

        class _R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            seen.update(json.loads(req.data.decode()))
            return _R()

        with mock.patch.object(integrations, "RESEND_API_KEY", "k"), \
                mock.patch("integrations.urllib.request.urlopen", fake_urlopen):
            integrations.send_html_email("a@b.co", "s", "t", "<p>h</p>", reply_to=HAND,
                                         attachments=[{"filename": "f.pdf", "content": "eA=="}])
        self.assertEqual(seen["reply_to"], HAND)
        self.assertEqual(seen["attachments"][0]["filename"], "f.pdf")

    def test_sendgrid_payload(self):
        seen = {}

        class _R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            seen.update(json.loads(req.data.decode()))
            return _R()

        with mock.patch.object(integrations, "RESEND_API_KEY", ""), \
                mock.patch.object(integrations, "SENDGRID_API_KEY", "k"), \
                mock.patch("integrations.urllib.request.urlopen", fake_urlopen):
            integrations.send_html_email("a@b.co", "s", "t", "<p>h</p>", reply_to=HAND,
                                         attachments=[{"filename": "f.pdf", "content": "eA=="}])
        self.assertEqual(seen["reply_to"], {"email": HAND})
        self.assertEqual(seen["attachments"][0]["type"], "application/pdf")
        # Default stays as before: no reply_to key at all.
        seen.clear()
        with mock.patch.object(integrations, "RESEND_API_KEY", ""), \
                mock.patch.object(integrations, "SENDGRID_API_KEY", "k"), \
                mock.patch("integrations.urllib.request.urlopen", fake_urlopen):
            integrations.send_html_email("a@b.co", "s", "t", "<p>h</p>")
        self.assertNotIn("reply_to", seen)


class Unit(unittest.TestCase):
    def test_should_alert_unreadable(self):
        un = {"recognized": False}
        self.assertTrue(real_upload_alert.should_alert_unreadable(un, OUTSIDER))
        self.assertTrue(real_upload_alert.should_alert_unreadable(un, ""))
        self.assertFalse(real_upload_alert.should_alert_unreadable({"recognized": True}, OUTSIDER))
        self.assertFalse(real_upload_alert.should_alert_unreadable(un, OUTSIDER, is_demo=True))
        self.assertFalse(real_upload_alert.should_alert_unreadable(un, OUTSIDER, internal=True))
        self.assertFalse(real_upload_alert.should_alert_unreadable(un, " JoveBull396@Gmail.com "))

    def test_reply_to_for(self):
        with mock.patch.dict(os.environ, {"HAND_REVIEW_EMAIL": ""}):
            self.assertIsNone(tc_check_email.reply_to_for({"recognized": False}))
        with mock.patch.dict(os.environ, {"HAND_REVIEW_EMAIL": HAND}):
            self.assertEqual(tc_check_email.reply_to_for({"recognized": False}), HAND)
            self.assertIsNone(tc_check_email.reply_to_for({"recognized": True}))

    def test_line_is_word_for_word(self):
        self.assertEqual(tc_check_email.HAND_REVIEW_LINE, "Send it, we'll check it by hand.")


if __name__ == "__main__":
    unittest.main()
