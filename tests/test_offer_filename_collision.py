"""Two offers for the same street in the same second must never share a file.

Found 2026-10-12: filenames were TREC20-19_<street>_<YYYYmmdd_HHMMSS>.pdf, so
two agents texting the same street within one second got the same name. The
second PDF overwrote the first on disk and record_offer() skipped the second
row, so agent A's /review link opened agent B's contract.

    python3 -m unittest tests.test_offer_filename_collision
"""
import os
import tempfile
import unittest
from unittest import mock

# Left in place after the run: offers_db binds DB_PATH at first import, and
# test modules that load later in the same run reuse it.
_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import pdf_filler  # noqa: E402
from offers_db import init_offers_table, record_offer, get_offer_by_filename  # noqa: E402


def _parsed(price):
    down = int(price * 0.03)
    return {"address": "123 Main St", "price": price, "down_payment_pct": 0.03,
            "down_payment_amount": down, "loan_amount": price - down,
            "earnest_money": int(price * 0.01), "option_fee": 250, "close_days": 21}


class OfferFilenameCollision(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_offers_table()

    def test_same_street_same_second_gets_two_files_and_two_rows(self):
        # Pin the clock so both offers land in the same second, as in the bug.
        fixed = pdf_filler.datetime(2026, 10, 12, 10, 0, 0)
        with mock.patch.object(pdf_filler, "datetime", wraps=pdf_filler.datetime) as dt:
            dt.now.return_value = fixed
            path_a = pdf_filler.fill_offer_pdf(_parsed(725000), "+15125550101")
            path_b = pdf_filler.fill_offer_pdf(_parsed(810000), "+15125550102")

        self.assertNotEqual(path_a, path_b)
        self.assertTrue(os.path.exists(path_a) and os.path.exists(path_b))

        record_offer("+15125550101", _parsed(725000), os.path.basename(path_a))
        record_offer("+15125550102", _parsed(810000), os.path.basename(path_b))
        a = get_offer_by_filename(os.path.basename(path_a))
        b = get_offer_by_filename(os.path.basename(path_b))
        self.assertEqual((a["phone"], a["price"]), ("+15125550101", 725000))
        self.assertEqual((b["phone"], b["price"]), ("+15125550102", 810000))

    def test_never_overwrites_an_existing_file(self):
        out = os.environ["OFFER_OUTPUT_DIR"]
        # Force the random suffix to repeat once: the first name is taken,
        # so the helper must retry with a new one, not overwrite.
        with mock.patch.object(pdf_filler.secrets, "token_hex", side_effect=["aaaaaa", "aaaaaa", "bbbbbb"]):
            f1, p1 = pdf_filler.open_unique_output(out, "TREC20-19_123MainSt")
            with f1:
                f1.write(b"first")
            f2, p2 = pdf_filler.open_unique_output(out, "TREC20-19_123MainSt")
            with f2:
                f2.write(b"second")
        self.assertNotEqual(p1, p2)
        self.assertTrue(p2.endswith("_bbbbbb.pdf"))
        import filecrypt
        self.assertEqual(filecrypt.read_file(p1), b"first")  # decrypts when FILE_ENCRYPTION_KEY is set


if __name__ == "__main__":
    unittest.main()
