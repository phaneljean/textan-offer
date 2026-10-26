"""/tc-check upload on phones (2026-10-26). Ad visitors from Facebook/
Instagram touched the drop box and no report followed, with nothing on
/analytics to say why. These keep: a real PDF accepted whatever the phone
names it (Android in-app pickers can send "msf:1000034"), every failure
pointing at the email route, and the picked/failed beacons reaching the
visitor rows.

    python3 -m unittest tests.test_upload_failures
"""
import io
import os
import re
import sqlite3
import subprocess
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import analytics
import app as app_module

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "static", "sample_trec_20-19.pdf")
UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Mobile Safari/537.36"}
FALLBACK = "tc@check.txtanoffer.com"


def _post(name, data, n=[0]):
    # A fresh client IP per post so the 20/IP limiter never trips.
    n[0] += 1
    return app_module.app.test_client().post(
        "/v1/tc/check", data={"file": (io.BytesIO(data), name), "source_page": "tc_check_page"},
        headers=UA, environ_base={"REMOTE_ADDR": f"10.9.0.{n[0]}"}, content_type="multipart/form-data")


class UploadRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SAMPLE, "rb") as f:
            cls.pdf = f.read()

    def test_pdf_without_pdf_name_is_checked(self):
        r = _post("msf:1000034", self.pdf)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        self.assertTrue(r.get_json().get("recognized"))

    def test_empty_file_says_so(self):
        r = _post("contract.pdf", b"")
        self.assertEqual(r.status_code, 400)
        self.assertIn("0 bytes", r.get_json()["error"])
        self.assertIn(FALLBACK, r.get_json()["error"])

    def test_not_a_pdf_rejected_with_fallback(self):
        for name in ("photo.jpg", "contract.pdf"):  # a misnamed JPEG too
            with self.subTest(name=name):
                r = _post(name, b"\xff\xd8\xff\xe0 not a pdf")
                self.assertEqual(r.status_code, 400)
                self.assertIn("Only PDF files", r.get_json()["error"])
                self.assertIn(FALLBACK, r.get_json()["error"])

    def test_unreadable_pdf_has_fallback(self):
        r = _post("contract.pdf", b"%PDF-1.7\ngarbage")
        self.assertEqual(r.status_code, 400)
        self.assertIn(FALLBACK, r.get_json()["error"])


class UploadPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = app_module.app.test_client().get("/tc-check", headers=UA).get_data(as_text=True)

    def test_accept_lists_mime_and_extension(self):
        self.assertIn('id="fileInput" accept="application/pdf,.pdf"', self.html)
        self.assertIn('id="addendumInput" accept="application/pdf,.pdf"', self.html)

    def test_every_beacon_the_upload_sends_is_accepted(self):
        sent = set(re.findall(r"beacon\('([a-z_]+)'\)", self.html)) | set(re.findall(r"'(upload_failed_[a-z]+)'", self.html))
        self.assertTrue({"upload_picked", "upload_failed_rejected", "upload_failed_network",
                         "upload_failed_timeout", "upload_failed_server"} <= sent, sent)
        for t in sent:
            self.assertIn(t, app_module._ENGAGEMENT_TYPES)
        self.assertIn("window.taEvt = evt", self.html)

    def test_client_failures_offer_email(self):
        self.assertIn("renderError(failReason", self.html)
        self.assertRegex(self.html, r"withFallback \? '<p class=\"result-fallback\">Still stuck\? Email the PDF to <a href=\"mailto:tc@check")

    def test_inline_js_parses(self):
        scripts = re.findall(r"<script>(.*?)</script>", self.html, re.S)
        path = os.path.join(_TMP, "page.js")
        for i, js in enumerate(scripts):
            with self.subTest(script=i):
                with open(path, "w") as f:
                    f.write(js)
                r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr[:400])


class VisitorRowShowsWhatHappened(unittest.TestCase):
    def setUp(self):
        conn = sqlite3.connect(analytics.DB_PATH)
        conn.execute("DELETE FROM events")
        conn.commit()
        conn.close()

    def _visit(self, vid, *after):
        analytics.track_event("page_view", None, {"visitor": vid, "page": "tc_check_page", "source": "ad_x_fb",
                                                  "device": "mobile", "ua": UA["User-Agent"]})
        for e in after:
            if e == "tc_check_attempted":
                analytics.track_event(e, None, {"visitor": vid, "source": "ad_x_fb"})
            else:
                analytics.track_event("page_engagement", None, {"type": e, "page": "tc_check_page", "visitor": vid})

    def test_rows(self):
        self._visit("a" * 32, "dropzone_interact", "upload_picked", "upload_failed_network")
        self._visit("b" * 32, "dropzone_interact", "upload_picked", "tc_check_attempted")
        rv = {v["visitor"]: v["events"] for v in analytics.get_recent_visitors(hours=48)}
        self.assertIn("upload_failed_network", rv["a" * 32])
        self.assertNotIn("upload_reached_server", rv["a" * 32])
        self.assertIn("upload_reached_server", rv["b" * 32])
        old = app_module.ANALYTICS_PASSWORD
        app_module.ANALYTICS_PASSWORD = "pw"
        try:
            html = app_module.app.test_client().get("/analytics?token=pw", headers=UA).get_data(as_text=True)
        finally:
            app_module.ANALYTICS_PASSWORD = old
        self.assertIn("picked a file, upload failed (network)", html)
        self.assertIn("upload reached server", html)


if __name__ == "__main__":
    unittest.main()
