"""Encryption at rest for the brokerage archive (2026-10-24): with
FILE_ENCRYPTION_KEY set, archived PDFs are AES-256-GCM ciphertext on disk,
brokers still download the exact original bytes, files saved before the key
existed are converted only after a verified round trip, and a missing or
wrong key fails loudly instead of serving garbage.

    python3 -m unittest tests.test_filecrypt
"""
import glob
import hashlib
import io
import os
import tempfile
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
import archive  # noqa: E402
import broker_auth  # noqa: E402
import brokerages  # noqa: E402
import filecrypt  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "static", "sample_trec_20-19.pdf"), "rb") as _f:
    PDF = _f.read()
KEY = filecrypt.new_key()
OTHER_KEY = filecrypt.new_key()


def _with_key(key=KEY):
    return mock.patch.dict(os.environ, {"FILE_ENCRYPTION_KEY": key})


def _on_disk(rec):
    with open(os.path.join(archive.ARCHIVE_DIR, str(rec["brokerage_id"]), rec["stored_name"]), "rb") as f:
        return f.read()


class FileCryptTests(unittest.TestCase):
    def test_round_trip_hides_the_pdf(self):
        with _with_key():
            sealed = filecrypt.encrypt(PDF)
            self.assertTrue(sealed.startswith(filecrypt.MAGIC))
            self.assertNotIn(b"%PDF", sealed)
            self.assertEqual(filecrypt.decrypt(sealed), PDF)
            self.assertNotEqual(filecrypt.encrypt(PDF), sealed)  # fresh nonce each time

    def test_encrypt_is_idempotent(self):
        with _with_key():
            sealed = filecrypt.encrypt(PDF)
            self.assertEqual(filecrypt.encrypt(sealed), sealed)

    def test_no_key_means_plaintext_both_ways(self):
        self.assertFalse(filecrypt.enabled())
        self.assertEqual(filecrypt.encrypt(PDF), PDF)
        self.assertEqual(filecrypt.decrypt(PDF), PDF)

    def test_encrypted_file_without_key_fails_loudly(self):
        with _with_key():
            sealed = filecrypt.encrypt(PDF)
        with self.assertRaises(filecrypt.FileCryptError):
            filecrypt.decrypt(sealed)

    def test_wrong_key_and_tampering_fail(self):
        with _with_key():
            sealed = filecrypt.encrypt(PDF)
        with _with_key(OTHER_KEY), self.assertRaises(filecrypt.FileCryptError):
            filecrypt.decrypt(sealed)
        broken = sealed[:-1] + bytes([sealed[-1] ^ 1])
        with _with_key(), self.assertRaises(filecrypt.FileCryptError):
            filecrypt.decrypt(broken)

    def test_malformed_key_is_rejected(self):
        for bad in ("not-base64!!", filecrypt.new_key()[:20]):
            with _with_key(bad), self.assertRaises(filecrypt.FileCryptError):
                filecrypt.encrypt(PDF)


class ArchiveEncryptionTests(unittest.TestCase):
    def setUp(self):
        self.b = brokerages.create_brokerage("Encrypt Test Realty", tc_email="tc@encrypt-test.example")
        self.client = app_module.app.test_client()
        self.client.set_cookie(broker_auth.COOKIE, broker_auth.session_value(self.b["id"], "tc@encrypt-test.example"))

    def _save(self, data=PDF, name="123 Main St.pdf"):
        return archive.save_file(self.b["id"], data, name, "upload", "tc@encrypt-test.example", {})

    def test_saved_file_is_ciphertext_on_disk(self):
        with _with_key():
            rec = self._save()
            raw = _on_disk(rec)
            self.assertTrue(filecrypt.is_encrypted(raw))
            self.assertNotIn(b"%PDF", raw)
            self.assertEqual(archive.read_file(archive.get_file(rec["id"], self.b["id"])), PDF)

    def test_duplicate_detection_still_works(self):
        with _with_key():
            self._save()
            self.assertTrue(self._save()["duplicate"])

    def test_no_temp_files_left_behind(self):
        with _with_key():
            self._save(PDF + b"\n% tmp-check")
        self.assertEqual(glob.glob(os.path.join(archive.ARCHIVE_DIR, "*", "*.tmp")), [])

    def test_download_returns_original_pdf(self):
        with _with_key():
            rec = self._save(PDF + b"\n% download")
            r = self.client.get(f"/broker/archive/file/{rec['id']}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, PDF + b"\n% download")
        self.assertEqual(r.mimetype, "application/pdf")
        self.assertIn("attachment", r.headers["Content-Disposition"])
        self.assertIn("123 Main St.pdf", r.headers["Content-Disposition"])
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")

    def test_other_brokerage_cannot_download(self):
        with _with_key():
            rec = self._save(PDF + b"\n% private")
        other = brokerages.create_brokerage("Other Realty", tc_email="tc@other-test.example")
        c = app_module.app.test_client()
        c.set_cookie(broker_auth.COOKIE, broker_auth.session_value(other["id"], "tc@other-test.example"))
        self.assertEqual(c.get(f"/broker/archive/file/{rec['id']}").status_code, 404)

    def test_export_zip_contains_plaintext(self):
        with _with_key():
            self._save(PDF + b"\n% export")
            r = self.client.get("/broker/archive/export.zip")
        self.assertEqual(r.status_code, 200)
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        executed = [n for n in zf.namelist() if n.startswith("executed/")]
        self.assertEqual(len(executed), 1)
        self.assertEqual(zf.read(executed[0]), PDF + b"\n% export")

    def test_legacy_plaintext_still_downloads_after_key_is_set(self):
        rec = self._save(PDF + b"\n% legacy-read")  # saved before the key existed
        self.assertEqual(_on_disk(rec), PDF + b"\n% legacy-read")
        with _with_key():
            r = self.client.get(f"/broker/archive/file/{rec['id']}")
        self.assertEqual(r.data, PDF + b"\n% legacy-read")


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.b = brokerages.create_brokerage("Legacy Realty", tc_email="tc@legacy-test.example")

    def _save(self, data):
        return archive.save_file(self.b["id"], data, "old.pdf", "upload", "", {})

    def test_noop_without_key(self):
        rec = self._save(PDF + b"\n% noop")
        self.assertEqual(archive.encrypt_legacy_files()["encrypted"], 0)
        self.assertEqual(_on_disk(rec), PDF + b"\n% noop")

    def test_converts_verifies_and_is_repeatable(self):
        good = self._save(PDF + b"\n% migrate")
        altered = self._save(PDF + b"\n% altered")
        path = os.path.join(archive.ARCHIVE_DIR, str(self.b["id"]), altered["stored_name"])
        with open(path, "ab") as f:
            f.write(b"changed after upload")
        gone = self._save(PDF + b"\n% gone")
        os.remove(os.path.join(archive.ARCHIVE_DIR, str(self.b["id"]), gone["stored_name"]))

        with _with_key():
            first = archive.encrypt_legacy_files()
            self.assertGreaterEqual(first["encrypted"], 1)
            self.assertGreaterEqual(first["mismatch"], 1)
            self.assertGreaterEqual(first["missing"], 1)
            raw = _on_disk(good)
            self.assertTrue(filecrypt.is_encrypted(raw))
            self.assertEqual(hashlib.sha256(filecrypt.decrypt(raw)).hexdigest(), good["sha256"])
            # A file that no longer matches its upload is left exactly as found.
            with open(path, "rb") as f:
                self.assertTrue(f.read().endswith(b"changed after upload"))
            second = archive.encrypt_legacy_files()
            self.assertEqual(second["encrypted"], 0)


if __name__ == "__main__":
    unittest.main()
