"""Generated offers encrypted at rest (2026-10-24): with FILE_ENCRYPTION_KEY
set, a generated PDF never lands on disk as plaintext, yet the signed link,
preview, validator, TC Check, email attachment and export all still see the
real PDF; offers saved before the key existed are converted with their
mtime kept, so the 30-day retention clock doesn't restart.

    python3 -m unittest tests.test_offer_encryption
"""
import base64
import io
import os
import tempfile
import time
import unittest
import zipfile
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
os.environ["ANALYTICS_PASSWORD"] = "test-internal"
os.environ.pop("FILE_ENCRYPTION_KEY", None)
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import app as app_module  # noqa: E402
import cleanup  # noqa: E402
import filecrypt  # noqa: E402
import integrations  # noqa: E402
import pdf_filler  # noqa: E402
from pdf_validator import validate_offer_pdf  # noqa: E402
from tc_audit import check_tc_file  # noqa: E402

OUT = os.environ["OFFER_OUTPUT_DIR"]
KEY = filecrypt.new_key()
PARSED = {"address": "123 Main St", "price": 725000, "down_payment_pct": 0.03,
          "down_payment_amount": 21750, "loan_amount": 703250,
          "earnest_money": 7250, "option_fee": 250, "close_days": 21}


def _with_key():
    return mock.patch.dict(os.environ, {"FILE_ENCRYPTION_KEY": KEY})


def _raw(path):
    with open(path, "rb") as f:
        return f.read()


class GeneratedOfferEncryption(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with _with_key():
            cls.path = pdf_filler.fill_offer_pdf(dict(PARSED), "+15125550101")
        cls.name = os.path.basename(cls.path)

    def test_never_plaintext_on_disk(self):
        raw = _raw(self.path)
        self.assertTrue(filecrypt.is_encrypted(raw))
        self.assertNotIn(b"%PDF", raw)
        self.assertNotIn(b"123 Main St", raw)

    def test_without_key_offers_stay_plaintext(self):
        path = pdf_filler.fill_offer_pdf(dict(PARSED), "+15125550102")
        self.assertTrue(_raw(path).startswith(b"%PDF"))

    def test_signed_link_serves_the_pdf(self):
        exp, sig = app_module.sign_pdf_view_params(self.name)
        with _with_key():
            r = app_module.app.test_client().get(f"/offers/{self.name}?expires={exp}&sig={sig}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data.startswith(b"%PDF"))
        self.assertEqual(r.mimetype, "application/pdf")
        self.assertIn("no-store", r.headers.get("Cache-Control", ""))

    def test_unsigned_link_still_refused(self):
        with _with_key():
            r = app_module.app.test_client().get(f"/offers/{self.name}")
        self.assertEqual(r.status_code, 403)

    def test_preview_renders(self):
        exp, sig = app_module.sign_pdf_view_params(self.name)
        with _with_key():
            r = app_module.app.test_client().get(f"/offers/{self.name}/preview.png?expires={exp}&sig={sig}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data.startswith(b"\x89PNG"))

    def test_validator_and_tc_check_read_it(self):
        with _with_key():
            v = validate_offer_pdf(self.path, {"price": 725000, "down_payment_amount": 21750,
                                               "loan_amount": 703250, "close_days": 21})
            audit = check_tc_file([self.path])
        self.assertNotIn("PDF file not found on server", v.get("blocking", []))
        self.assertTrue(audit.get("recognized"))

    def test_email_attachment_is_the_real_pdf(self):
        sent = {}

        def fake_resend(to, subject, body, attachments=None):
            sent["att"] = attachments
            return {"success": True}

        with _with_key(), mock.patch.object(integrations, "RESEND_API_KEY", "x"), \
                mock.patch.object(integrations, "_send_via_resend", side_effect=fake_resend):
            integrations.send_offer_email("listing@example.com", self.path, dict(PARSED))
        self.assertTrue(base64.b64decode(sent["att"][0]["content"]).startswith(b"%PDF"))


class LegacyOfferEncryption(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _plain(self, name, age_days=0):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(b"%PDF-1.7 legacy " + name.encode())
        t = time.time() - age_days * 86400
        os.utime(path, (t, t))
        return path

    def test_noop_without_key(self):
        self._plain("old.pdf")
        self.assertEqual(cleanup.encrypt_legacy_offers(self.dir), 0)

    def test_converts_keeps_mtime_and_is_repeatable(self):
        pdf = self._plain("old.pdf", age_days=20)
        mtime = os.path.getmtime(pdf)
        other = os.path.join(self.dir, "notes.db")
        with open(other, "wb") as f:
            f.write(b"not a pdf")
        with _with_key():
            self.assertEqual(cleanup.encrypt_legacy_offers(self.dir), 1)
            self.assertTrue(filecrypt.is_encrypted(_raw(pdf)))
            self.assertEqual(filecrypt.read_file(pdf), b"%PDF-1.7 legacy old.pdf")
            self.assertEqual(cleanup.encrypt_legacy_offers(self.dir), 0)
        self.assertAlmostEqual(os.path.getmtime(pdf), mtime, delta=1)
        self.assertEqual(_raw(other), b"not a pdf")
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".tmp")], [])

    def test_retention_still_deletes_old_encrypted_offers(self):
        pdf = self._plain("expired.pdf", age_days=40)
        with _with_key():
            cleanup.encrypt_legacy_offers(self.dir)
        self.assertEqual(cleanup.cleanup_old_pdfs(self.dir), 1)
        self.assertFalse(os.path.exists(pdf))


if __name__ == "__main__":
    unittest.main()
