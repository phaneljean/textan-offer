"""Real-upload alert (real_upload_alert.py): fires for an outside sender's
filled 20-19 on both the web upload and the email-forward path, and stays
quiet for demo runs, Phanel's own traffic and blank drafts.

    python3 -m unittest tests.test_real_upload_alert
"""
import io
import os
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["TC_CHECK_EMAIL_TOKEN"] = "test-token"
os.environ["ANALYTICS_PASSWORD"] = "test-internal"

import real_upload_alert  # noqa: E402
from tc_audit import check_tc_file  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
PLANTED = os.path.join(HERE, "fixtures", "trec_20-19_planted_errors.pdf")
CLEAN = os.path.join(HERE, "fixtures", "trec_20-19_clean_two_parties.pdf")
BLANK = os.path.join(ROOT, "20-19_2.pdf")  # TREC's own empty template
OUTSIDER = "jane@brokerage-example.com"


class _InlineThread:
    """Runs the alert send inline so the test can see it."""
    def __init__(self, target, daemon=None):
        self.target = target

    def start(self):
        self.target()


class ShouldAlert(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.filled = check_tc_file([CLEAN])
        cls.blank = check_tc_file([BLANK])

    def test_outside_filled_file_alerts(self):
        self.assertTrue(real_upload_alert.should_alert(self.filled, OUTSIDER))
        self.assertTrue(real_upload_alert.should_alert(self.filled, ""))  # anonymous web upload

    def test_demo_internal_and_own_emails_do_not(self):
        self.assertFalse(real_upload_alert.should_alert(self.filled, OUTSIDER, is_demo=True))
        self.assertFalse(real_upload_alert.should_alert(self.filled, OUTSIDER, internal=True))
        for own in ("pejeanbaptiste@gmail.com", " JoveBull396@Gmail.com "):
            self.assertFalse(real_upload_alert.should_alert(self.filled, own), own)

    def test_blank_and_unrecognized_do_not(self):
        self.assertTrue(self.blank["recognized"])
        self.assertFalse(real_upload_alert.should_alert(self.blank, OUTSIDER))
        self.assertFalse(real_upload_alert.should_alert({"recognized": False, "issues": []}, OUTSIDER))
        no_address = dict(self.filled, property={"address": "", "city": "", "county": ""})
        self.assertFalse(real_upload_alert.should_alert(no_address, OUTSIDER))

    def test_alert_body(self):
        subject, body = real_upload_alert.format_alert(check_tc_file([PLANTED]), "email forward", "email", OUTSIDER)
        self.assertIn(OUTSIDER, subject)
        for part in ("Source: email forward", "src tag: email", "Recognized: yes", "Issues: "):
            self.assertIn(part, body)


@mock.patch("real_upload_alert.threading.Thread", _InlineThread)
@mock.patch("real_upload_alert.send_plain_email")
class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        cls.app = app_module.app
        cls.app.config["TESTING"] = True
        # Report emails to the sender are out of scope here.
        cls._p = [mock.patch("app.send_html_email"), mock.patch("app.check_and_increment", return_value=True)]
        for p in cls._p:
            p.start()

    @classmethod
    def tearDownClass(cls):
        for p in cls._p:
            p.stop()

    def _web(self, path, demo=False, cookie=None):
        c = self.app.test_client()
        if cookie:
            c.set_cookie(*cookie)
        c.set_cookie("ta_src", "ti_keydfw")
        with open(path, "rb") as fh:
            data = {"file": (io.BytesIO(fh.read()), "contract.pdf"), "source_page": "tc_check_page"}
        if demo:
            data["is_demo"] = "1"
        r = c.post("/v1/tc/check", data=data, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)

    def _forward(self, path, sender):
        with open(path, "rb") as fh:
            data = {"from": f"Jane <{sender}>", "SPF": "pass", "attachments": "1",
                    "attachment1": (io.BytesIO(fh.read()), "contract.pdf")}
        r = self.app.test_client().post("/v1/tc/check/email/test-token", data=data, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)

    def test_web_upload_alerts_with_src(self, send):
        self._web(CLEAN)
        send.assert_called_once()
        to, subject, body = send.call_args[0]
        self.assertEqual(to, "support@txtanoffer.com")
        self.assertIn("src tag: ti_keydfw", body)
        self.assertIn("web upload (tc_check_page)", body)

    def test_web_demo_blank_and_internal_are_quiet(self, send):
        self._web(CLEAN, demo=True)
        self._web(BLANK)
        self._web(CLEAN, cookie=("ta_internal", "test-internal"))
        send.assert_not_called()

    def test_email_forward_alerts(self, send):
        self._forward(CLEAN, OUTSIDER)
        send.assert_called_once()
        self.assertIn(f"Sender: {OUTSIDER}", send.call_args[0][2])

    def test_email_forward_from_phanel_is_quiet(self, send):
        self._forward(CLEAN, "pejeanbaptiste@gmail.com")
        self._forward(BLANK, OUTSIDER)
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
