"""Paragraph 9A closing date: one source of truth (deadlines.offer_closing_date).

Found 2026-10-24: pdf_filler stamped server-local now + close_days while the
review page and pdf_validator used created_at (stored UTC) + close_days. On a
non-UTC host, any offer drafted after 5 PM Pacific got Nov 14 on the PDF and
Nov 15 (a Sunday) on the review page, and the validator blocked "Email to
Listing Agent". These tests freeze the clock and drive the real SMS flow, then
check the SMS confirmation, the PDF overlay, the validator, the review page
and the 39-11 all show the same date.

    python3 -m unittest tests.test_closing_date
"""
import os
import re
import tempfile

import unittest
from datetime import date, datetime, timezone
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import deadlines
from deadlines import offer_closing_date, texas_date

UTC = timezone.utc

# (name, frozen UTC moment, close_days, expected closing date)
CASES = [
    # Wed Oct 21, 10 AM Central + 20 = Tue Nov 10: a plain weekday, no shift.
    ("weekday", datetime(2026, 10, 21, 15, 0, tzinfo=UTC), 20, date(2026, 11, 10)),
    # Fri Oct 23 + 22 = Sat Nov 14 -> Mon Nov 16.
    ("saturday", datetime(2026, 10, 23, 15, 0, tzinfo=UTC), 22, date(2026, 11, 16)),
    # Fri Oct 23 + 23 = Sun Nov 15 -> Mon Nov 16.
    ("sunday", datetime(2026, 10, 23, 15, 0, tzinfo=UTC), 23, date(2026, 11, 16)),
    # Wed Oct 21 + 21 = Wed Nov 11, Veterans Day -> Thu Nov 12.
    ("holiday", datetime(2026, 10, 21, 15, 0, tzinfo=UTC), 21, date(2026, 11, 12)),
    # 01:30 UTC Sat Oct 24 is still Fri Oct 23, 8:30 PM in Texas. + 20 =
    # Thu Nov 12 (UTC-date math would give Fri Nov 13).
    ("tz_edge", datetime(2026, 10, 24, 1, 30, tzinfo=UTC), 20, date(2026, 11, 12)),
    # The live repro: Sat Oct 24, 7:07 PM Central (00:07 UTC Sun) + 21 =
    # Sat Nov 14 -> Mon Nov 16. Was PDF Nov 14 vs review Nov 15.
    ("live_repro", datetime(2026, 10, 25, 0, 7, tzinfo=UTC), 21, date(2026, 11, 16)),
]

# What the SMS summary says about a rolled date (deadlines.roll_note).
NOTES = {"saturday": "moved off the weekend", "sunday": "moved off the weekend",
         "holiday": "moved off a holiday", "live_repro": "moved off the weekend"}


class _FrozenDatetime(datetime):
    """offers_db.record_offer stamps created_at with datetime.utcnow()."""
    frozen = None

    @classmethod
    def utcnow(cls):
        return cls.frozen.replace(tzinfo=None)


class OfferClosingDateUnit(unittest.TestCase):
    def test_cases_from_stored_created_at(self):
        for name, moment, days, expected in CASES:
            with self.subTest(name):
                # created_at is stored naive UTC; also accept aware datetimes.
                naive_iso = moment.replace(tzinfo=None).isoformat()
                self.assertEqual(offer_closing_date(days, naive_iso), expected)
                self.assertEqual(offer_closing_date(days, moment), expected)

    def test_cases_from_now(self):
        for name, moment, days, expected in CASES:
            with self.subTest(name), mock.patch("deadlines._utcnow", return_value=moment):
                self.assertEqual(offer_closing_date(days), expected)

    def test_texas_date_cst_after_dst_ends(self):
        # Nov 10, 05:30 UTC = Nov 9, 11:30 PM CST (UTC-6, DST ended Nov 1).
        self.assertEqual(texas_date("2026-11-10T05:30:00"), date(2026, 11, 9))
        self.assertEqual(texas_date("2026-11-10T06:30:00"), date(2026, 11, 10))

    def test_roll_note(self):
        from deadlines import roll_note
        self.assertEqual(roll_note(date(2026, 11, 10)), "")                    # Tue
        self.assertEqual(roll_note(date(2026, 11, 14)), "moved off the weekend")  # Sat
        self.assertEqual(roll_note(date(2026, 11, 11)), "moved off a holiday")    # Veterans Day
        # Sat Jan 16, 2027 -> Sun -> MLK Mon Jan 18 -> Tue Jan 19
        self.assertEqual(roll_note(date(2027, 1, 16)), "moved off the weekend and a holiday")

    def test_thanksgiving(self):
        # Thu Oct 29 + 28 = Thu Nov 26, Thanksgiving -> Fri Nov 27.
        self.assertEqual(offer_closing_date(28, "2026-10-29T15:00:00"), date(2026, 11, 27))

    def test_bad_created_at_falls_back_to_now(self):
        with mock.patch("deadlines._utcnow", return_value=CASES[0][1]):
            self.assertEqual(offer_closing_date(20, "not a date"), CASES[0][3])


class ClosingDateSurfacesAgree(unittest.TestCase):
    """SMS confirmation, PDF overlay + cover page, validator, review page and
    the 39-11 all print the same closing date for a frozen drafting moment."""

    @classmethod
    def setUpClass(cls):
        import app as app_module
        app_module.app.config["TESTING"] = True
        cls.app_module = app_module
        cls.client = app_module.app.test_client()
        cls._phones = iter(range(300, 400))

    def _text_offer(self, n, days):
        from agent_profiles import save_agent_profile
        phone = f"+1512555{next(self._phones):04d}"
        save_agent_profile(phone, {
            "name": "Jane Agent", "license": "0123456", "phone": phone,
            "email": "jane@example.com", "brokerage": "Example Realty",
            "business_address": "123 Main St, Austin, TX 78701",
            "title_company": "Example Title", "title_company_address": "123 Main St, Austin, TX 78701",
        })
        sent = []
        # A distinct street number per case keeps offer filenames
        # (street + 1-second timestamp) from colliding.
        with mock.patch("app.twilio_send_sms", side_effect=lambda to, body: sent.append(body)):
            for body in (f"725k 3% {days}day {n} Main St, Austin, TX Travis", "YES"):
                self.assertEqual(self.client.post("/sms", data={"From": phone, "Body": body}).status_code, 200)
        return sent

    def test_all_surfaces_agree(self):
        from pypdf import PdfReader
        import filecrypt
        from pdf_validator import validate_offer_pdf
        from offers_db import get_offer_by_filename
        from amendment import fill_amendment_pdf

        for i, (name, moment, days, expected) in enumerate(CASES):
            with self.subTest(name):
                _FrozenDatetime.frozen = moment
                with mock.patch("deadlines._utcnow", return_value=moment), \
                        mock.patch("offers_db.datetime", _FrozenDatetime):
                    sent = self._text_offer(201 + i, days)
                    joined = "\n".join(sent)

                    # SMS confirmation: the date, and why it moved when it did.
                    note = NOTES.get(name, "")
                    self.assertIn(f"Closing {expected:%a}, {expected:%b} {expected.day}, {expected.year} "
                                  f"({days} days{', ' + note if note else ''})", joined)

                    m = re.search(r"http://localhost(/review/\S+)", joined)
                    self.assertIsNotNone(m, joined)
                    path = m.group(1)
                    filename = path.split("/review/")[1].split("?")[0]
                    offer = get_offer_by_filename(filename)
                    self.assertEqual(offer["created_at"], moment.replace(tzinfo=None).isoformat())

                    # PDF: 9A overlay (and the cover page tile)
                    pdf_path = os.path.join(self.app_module.OUTPUT_DIR, filename)  # bound at import; discover may set it first
                    reader = PdfReader(filecrypt.pdf_source(pdf_path))
                    p6 = reader.pages[6].extract_text()
                    self.assertIn(expected.strftime("%B %d,"), p6)
                    self.assertIn(f"{expected:%B} {expected.day}, {expected.year}", reader.pages[0].extract_text())

                # Validator and review page run later, on a real clock: they
                # must anchor to created_at, not to whatever "now" is.
                price, down = offer["price"], int(offer["price"] * offer["down_pct"])
                v = validate_offer_pdf(pdf_path, {
                    "price": price, "down_payment_amount": down, "loan_amount": price - down,
                    "close_days": days, "created_at": offer["created_at"],
                    "financing_type_specified": bool(offer.get("financing_type")),
                })
                self.assertNotIn("Section 9: Closing Date", v["blocking"])

                html = self.client.get(path).get_data(as_text=True)
                self.assertIn(
                    f'<div class="stat-label">Close</div><div class="stat-value">{expected:%B} {expected.day}, {expected.year}</div>',
                    html)
                self.assertNotIn("Section 9: Closing Date", html)
                self.assertIn('id="email-toggle">', html)  # enabled, not disabled

                # 39-11: extending by 1 day starts from the date the 20-19 printed.
                amend_path = fill_amendment_pdf(offer, {"field": "close", "value": 1})
                fields = PdfReader(filecrypt.pdf_source(amend_path)).get_fields()
                new_close = deadlines.add_trec_days(expected, 1)
                self.assertEqual(str(fields["date 5"].get("/V")), new_close.strftime("%B %d,"))


if __name__ == "__main__":
    unittest.main()
