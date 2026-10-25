"""Which 20-19 deadlines move off a weekend/holiday, and which don't.

TREC 20-19 (05-04-2026, read from 20-19_2.pdf): the only extension is
Paragraph 5A(2) -- earnest money, option fee and additional earnest money
delivery moves to the end of the next day that isn't a Saturday, Sunday or
Legal Holiday ("Legal Holiday" = Tex. Gov't Code §662.003(a) + June 19 + the
Friday after Thanksgiving; not Columbus Day). 5B (option period) and 9A
(closing) are not extended. The 9A roll is TxtAnOffer's drafting default.

trec-qa re-review 2026-10-24 found:
  P1  option_end_date rolled 5B, so a period ending Sat Oct 31 showed Mon Nov 2.
  P2  earnest_money_deadline used the federal set: Columbus Day extended 5A,
      the Friday after Thanksgiving didn't.
  M1  the 39-11 SMS said "Closing extended N days" while the PDF printed a
      rolled date further out.
  M2  offers generated before GENERATOR_VERSION 2026-10-24.1 printed the
      unrolled UTC date, but every page now recomputed the new rule, so the
      validator blocked the Email button on them.
  CO  _build_co_draft used the host clock, not Denver's date.

    python3 -m unittest tests.test_deadline_rules
"""
import os
import re
import sqlite3
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import deadlines
from deadlines import (earnest_money_deadline, option_end_date, fmt_option_end, offer_closing_date,
                       add_trec_days, build_day_one_summary)

UTC = timezone.utc
OPTION_LINE = "Option period ends: 5:00 PM local time, Sat, Oct 31, 2026 (Par. 5B, not extended for weekends/holidays)"


class _FrozenDatetime(datetime):
    """offers_db / app stamp times with datetime.utcnow()."""
    frozen = None

    @classmethod
    def utcnow(cls):
        return cls.frozen.replace(tzinfo=None)

    @classmethod
    def now(cls, tz=None):
        return cls.frozen.astimezone(tz) if tz else cls.frozen.replace(tzinfo=None)


class OptionPeriodNotExtended(unittest.TestCase):
    """P1: 5B is Effective Date + N, never moved."""

    def test_saturday_end_stays_saturday(self):
        # Wed Oct 21, 2026 + 10 = Sat Oct 31.
        self.assertEqual(option_end_date(date(2026, 10, 21), 10), date(2026, 10, 31))

    def test_holiday_end_stays(self):
        # Mon Nov 16 + 10 = Thu Nov 26, Thanksgiving: still Nov 26.
        self.assertEqual(option_end_date(date(2026, 11, 16), 10), date(2026, 11, 26))

    def test_no_option(self):
        self.assertIsNone(option_end_date(date(2026, 10, 21), None))
        self.assertIsNone(option_end_date(date(2026, 10, 21), 0))

    def test_display(self):
        self.assertEqual(fmt_option_end(date(2026, 10, 31)),
                         "5:00 PM local time, Sat, Oct 31, 2026 (Par. 5B, not extended for weekends/holidays)")
        lines = build_day_one_summary("123 Main St", date(2026, 10, 21), 10, None)
        self.assertIn(OPTION_LINE, lines)

    def test_transaction_task_due_date(self):
        import transaction_tasks
        self.assertEqual(transaction_tasks._resolve_due_date("option_end", date(2026, 10, 21), 10, None),
                         date(2026, 10, 31))


class EarnestMoney5A2(unittest.TestCase):
    """P2: 5A(2) uses the Texas Legal Holiday set, not the federal one."""

    def test_columbus_day_is_not_a_legal_holiday(self):
        # Fri Oct 9, 2026 + 3 = Mon Oct 12 (Columbus Day, federal only): due that day.
        self.assertEqual(earnest_money_deadline(date(2026, 10, 9)), date(2026, 10, 12))

    def test_friday_after_thanksgiving(self):
        # Mon Nov 23 + 3 = Thu Nov 26 Thanksgiving -> Fri 27 (b)(6) -> Sat, Sun -> Mon Nov 30.
        self.assertEqual(earnest_money_deadline(date(2026, 11, 23)), date(2026, 11, 30))
        # Tue Nov 24 + 3 = Fri Nov 27 itself -> Mon Nov 30.
        self.assertEqual(earnest_money_deadline(date(2026, 11, 24)), date(2026, 11, 30))

    def test_juneteenth(self):
        # Tue Jun 16, 2026 + 3 = Fri Jun 19 (b)(4) -> Mon Jun 22.
        self.assertEqual(earnest_money_deadline(date(2026, 6, 16)), date(2026, 6, 22))

    def test_weekend(self):
        # Wed Oct 21 + 3 = Sat Oct 24 -> Mon Oct 26.
        self.assertEqual(earnest_money_deadline(date(2026, 10, 21)), date(2026, 10, 26))

    def test_additional_earnest_money_same_rule(self):
        # Mon Nov 16 + 11 = Fri Nov 27 -> Mon Nov 30.
        self.assertEqual(earnest_money_deadline(date(2026, 11, 16), 11), date(2026, 11, 30))

    def test_9a_drafting_roll_keeps_federal_set(self):
        # The closing roll is a different (federal) set on purpose.
        self.assertEqual(add_trec_days(date(2026, 10, 9), 3), date(2026, 10, 13))       # Columbus Day rolls
        self.assertEqual(add_trec_days(date(2026, 11, 24), 3), date(2026, 11, 27))      # Fri after Thanksgiving doesn't

    def test_matches_public_calculator_holidays(self):
        # static/trec-deadlines.js is the public calculator; same list.
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        js = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "trec-deadlines.js")
        for year in (2026, 2027, 2028):
            out = subprocess.run([node, "-e", f"console.log(Object.keys(require({js!r}).legalHolidays({year})).sort().join(','))"],
                                 capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(out, ",".join(sorted(d.isoformat() for d in deadlines._tx_legal_holidays(year))))


class LegacyClosingDate(unittest.TestCase):
    """M2 unit: pre-2026-10-24.1 rows get the date their PDF printed."""

    def test_legacy_versions(self):
        # 01:30 UTC Sat Oct 24 (Fri 8:30 PM Central) + 21. Old filler on a UTC
        # host: Oct 24 + 21 = Sat Nov 14, not rolled. New rule: Fri Nov 13.
        at = "2026-10-24T01:30:00"
        self.assertEqual(offer_closing_date(21, at, "2026-10-12.2"), date(2026, 11, 14))
        self.assertEqual(offer_closing_date(21, at, ""), date(2026, 11, 14))     # row older than the column
        self.assertEqual(offer_closing_date(21, at, "2026-10-24.1"), date(2026, 11, 13))
        self.assertEqual(offer_closing_date(21, at, "2026-10-24.2"), date(2026, 11, 13))
        self.assertEqual(offer_closing_date(21, at), date(2026, 11, 13))         # not a stored row
        self.assertEqual(deadlines.closing_roll_note(21, at, "2026-10-12.2"), "")


class ColoradoDraftDate(unittest.TestCase):
    def test_evening_mountain_is_still_today(self):
        # 03:30 UTC Thu Oct 22 = Wed Oct 21, 9:30 PM MDT. Acceptance is the
        # next weekday after Denver's today: Thu Oct 22 (UTC-date math: Fri Oct 23).
        import app as app_module
        moment = datetime(2026, 10, 22, 3, 30, tzinfo=UTC)
        parsed = {"address": "77 Birch Ln, Boulder, CO 80302", "price": 500000, "down_payment_pct": 0.2,
                  "close_days": 30}
        with mock.patch("deadlines._utcnow", return_value=moment):
            self.assertEqual(deadlines.colorado_date(), date(2026, 10, 21))
            out = app_module._build_co_draft(parsed, "+15125559801")
            import co_filler
            self.assertEqual(co_filler.contract_dates(30), (date(2026, 10, 22), date(2026, 11, 20)))
        self.assertEqual(out["co_acceptance"], "Oct 22")
        self.assertEqual(out["co_closing"], "Nov 20")


class SurfacesViaSmsFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        app_module.app.config["TESTING"] = True
        cls.app_module = app_module
        cls.client = app_module.app.test_client()
        cls._phones = iter(range(500, 600))
        cls._last = 0.0

    def _text_offer(self, text, moment, legacy=False):
        """Text an offer at a frozen moment; legacy=True makes it the way a
        pre-2026-10-24.1 generator did (UTC date + N, no roll, old version
        stamped on the row and PDF)."""
        from agent_profiles import save_agent_profile
        from offers_db import get_offer_by_filename
        phone = f"+1512555{next(self._phones):04d}"
        save_agent_profile(phone, {
            "name": "Jane Agent", "license": "0123456", "phone": phone,
            "email": "jane@example.com", "brokerage": "Example Realty",
            "business_address": "123 Main St, Austin, TX 78701",
            "title_company": "Example Title", "title_company_address": "123 Main St, Austin, TX 78701",
        })
        time.sleep(max(0.0, 1.05 - (time.time() - SurfacesViaSmsFlow._last)))
        _FrozenDatetime.frozen = moment
        sent = []
        patches = [mock.patch("deadlines._utcnow", return_value=moment),
                   mock.patch("offers_db.datetime", _FrozenDatetime),
                   mock.patch("app.twilio_send_sms", side_effect=lambda to, body: sent.append(body))]
        if legacy:
            old = lambda days, drafted_at=None, gv=None: moment.date() + timedelta(days=int(days))
            patches += [mock.patch("pdf_filler.GENERATOR_VERSION", "2026-10-12.2"),
                        mock.patch("pdf_filler.offer_closing_date", old),
                        mock.patch("cover_page.offer_closing_date", old)]
        for p in patches:
            p.start()
        try:
            for body in (text, "YES"):
                self.assertEqual(self.client.post("/sms", data={"From": phone, "Body": body}).status_code, 200)
        finally:
            for p in reversed(patches):
                p.stop()
        SurfacesViaSmsFlow._last = time.time()
        m = re.search(r"http://localhost(/review/\S+)", "\n".join(sent))
        self.assertIsNotNone(m, sent)
        path = m.group(1)
        filename = path.split("/review/")[1].split("?")[0]
        return phone, path, filename, get_offer_by_filename(filename)

    def _amend(self, phone, moment, text):
        sent = []
        _FrozenDatetime.frozen = moment
        with mock.patch("deadlines._utcnow", return_value=moment), \
                mock.patch("offers_db.datetime", _FrozenDatetime), \
                mock.patch("app.twilio_send_sms", side_effect=lambda to, body: sent.append(body)):
            self.client.post("/sms", data={"From": phone, "Body": text})
        return "\n".join(sent)

    def _amend_pdf_date(self, sms):
        from pypdf import PdfReader
        import filecrypt
        m = re.search(r"/(?:offers|review)/(AMEND39-11_\S+?\.pdf)", sms)
        self.assertIsNotNone(m, sms)
        fields = PdfReader(filecrypt.pdf_source(os.path.join(self.app_module.OUTPUT_DIR, m.group(1)))).get_fields()
        return str(fields["date 5"].get("/V")), str(fields["20_25"].get("/V"))

    def test_39_11_holiday_landing(self):
        # M1: drafted Sat Oct 24, 2026 (10 AM Central) + 21 = Sat Nov 14 -> Mon
        # Nov 16 printed. AMEND close +10 = Thu Nov 26, Thanksgiving -> Fri Nov 27.
        moment = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)
        phone, _, _, offer = self._text_offer("725k 3% 21day 611 Main St, Austin, TX Travis", moment)
        self.assertEqual(offer_closing_date(21, offer["created_at"], offer["generator_version"]), date(2026, 11, 16))
        sms = self._amend(phone, moment, "AMEND 611 Main St close +10")
        self.assertIn("New closing date (Paragraph 9): Fri, Nov 27, 2026 (10 days later, moved off a holiday)", sms)
        self.assertNotIn("Closing extended", sms)
        self.assertEqual(self._amend_pdf_date(sms), ("November 27,", "26"))

    def test_39_11_no_roll(self):
        moment = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)  # Wed + 20 = Tue Nov 10
        phone, _, _, _ = self._text_offer("725k 3% 20day 612 Main St, Austin, TX Travis", moment)
        sms = self._amend(phone, moment, "AMEND 612 Main St close +2")
        self.assertIn("New closing date (Paragraph 9): Thu, Nov 12, 2026 (2 days later)", sms)

    def test_legacy_offer_pages_match_old_pdf(self):
        # M2: Fri Oct 23, 8:30 PM Central = 01:30 UTC Sat Oct 24. The old filler
        # printed Oct 24 + 21 = Sat Nov 14 (unrolled); the new rule would say Fri Nov 13.
        from pypdf import PdfReader
        import filecrypt
        import reminders
        from pdf_validator import validate_offer_pdf
        moment = datetime(2026, 10, 24, 1, 30, tzinfo=UTC)
        phone, path, filename, offer = self._text_offer(
            "725k 3% 21day 10 day option 613 Main St, Austin, TX Travis", moment, legacy=True)
        self.assertEqual(offer["generator_version"], "2026-10-12.2")
        pdf_path = os.path.join(self.app_module.OUTPUT_DIR, filename)
        self.assertIn("November 14,", PdfReader(filecrypt.pdf_source(pdf_path)).pages[6].extract_text())

        # Review page: same date as the PDF, no Section 9 block, Email enabled.
        html = self.client.get(path).get_data(as_text=True)
        self.assertIn('<div class="stat-label">Close</div><div class="stat-value">November 14, 2026</div>', html)
        self.assertNotIn("Section 9: Closing Date", html)
        self.assertIn('id="email-toggle">', html)
        price, down = offer["price"], int(offer["price"] * offer["down_pct"])
        v = validate_offer_pdf(pdf_path, {"price": price, "down_payment_amount": down, "loan_amount": price - down,
                                          "close_days": 21, "created_at": offer["created_at"],
                                          "generator_version": offer["generator_version"]})
        self.assertNotIn("Section 9: Closing Date", v["blocking"])

        # Accepted Mon Oct 26: the Day One SMS and thread page show the printed
        # closing date too.
        accept_at = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)
        _FrozenDatetime.frozen = accept_at
        sent = []
        expires = int(time.time()) + 3600
        thread = self.app_module.sign_thread_url(filename)
        with mock.patch("offers_db.datetime", _FrozenDatetime), mock.patch("app.datetime", _FrozenDatetime), \
                mock.patch("app.twilio_send_sms", side_effect=lambda to, body: sent.append(body)):
            r = self.client.post(thread, data={
                "action": "accept", "action_expires": str(expires),
                "action_sig": self.app_module.sign_thread_action(filename, "accept", expires)})
            self.assertIn(r.status_code, (200, 302, 303))
        summary = "\n".join(sent)
        self.assertIn("Closing on or before: Nov 14, 2026", summary)
        # Mon Oct 26 + 10 = Thu Nov 5, not moved; + 3 = Thu Oct 29.
        self.assertIn("Option period ends: 5:00 PM local time, Thu, Nov 5, 2026 (Par. 5B, not extended for weekends/holidays)", summary)
        self.assertIn("Earnest money + option fee due: Oct 29, 2026 (Par. 5)", summary)
        page = self.client.get(thread).get_data(as_text=True)
        self.assertIn("Closing on or before: Nov 14, 2026", page)

        # 39-11 starts from the printed Nov 14: + 1 = Sun Nov 15 -> Mon Nov 16.
        sms = self._amend(phone, accept_at, "AMEND 613 Main St close +1")
        self.assertIn("New closing date (Paragraph 9): Mon, Nov 16, 2026 (1 day later, moved off the weekend)", sms)
        self.assertEqual(self._amend_pdf_date(sms), ("November 16,", "26"))

        # Closing reminder fires 3 days before the printed date.
        class _At(datetime):
            @classmethod
            def utcnow(cls):
                return datetime(2026, 11, 11, 12, 0)
        with mock.patch("reminders.datetime", _At):
            due = [r for r in reminders._due_offers() if r["filename"] == filename]
        self.assertEqual([r["closing_date"] for r in due], [date(2026, 11, 14)])

    def test_new_offer_option_on_thread_transaction_and_reminder(self):
        # P1 end to end: drafted Wed Oct 21 + 20 = Tue Nov 10; accepted Wed
        # Oct 21, 10 day option -> Sat Oct 31, 5 PM, not moved.
        import reminders
        import transaction_tasks
        moment = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)
        phone, path, filename, offer = self._text_offer(
            "725k 3% 20day 10 day option 614 Main St, Austin, TX Travis", moment)
        self.assertEqual(offer["option_days"], 10)
        _FrozenDatetime.frozen = moment
        sent = []
        expires = int(time.time()) + 3600
        thread = self.app_module.sign_thread_url(filename)
        with mock.patch("offers_db.datetime", _FrozenDatetime), mock.patch("app.datetime", _FrozenDatetime), \
                mock.patch("app.twilio_send_sms", side_effect=lambda to, body: sent.append(body)):
            self.client.post(thread, data={
                "action": "accept", "action_expires": str(expires),
                "action_sig": self.app_module.sign_thread_action(filename, "accept", expires)})
        summary = "\n".join(sent)
        self.assertIn(OPTION_LINE, summary)
        self.assertIn("Earnest money + option fee due: Oct 26, 2026 (Par. 5)", summary)
        self.assertIn(OPTION_LINE, self.client.get(thread).get_data(as_text=True))

        tasks = transaction_tasks.get_tasks(filename)
        option_due = {t["due_date"] for t in tasks if t["stage"] == "Option Period"}
        self.assertEqual(option_due, {"2026-10-31"})
        self.assertEqual({t["due_date"] for t in tasks if t["stage"] == "Earnest Money"}, {"2026-10-26"})

        # Transaction page closing card (shown within 5 days of closing).
        later = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)
        _FrozenDatetime.frozen = later
        with mock.patch("app.datetime", _FrozenDatetime), mock.patch("app.has_professional_access", return_value=True):
            html = self.client.get(self.app_module.sign_transaction_url(filename)).get_data(as_text=True)
        self.assertIn(f"<div>{OPTION_LINE}</div>", html)
        self.assertIn("<div>Earnest money due: Oct 26, 2026</div>", html)

        # Option reminder 2 days before Sat Oct 31 (Thu Oct 29), with the 5B wording.
        class _At(datetime):
            @classmethod
            def utcnow(cls):
                return datetime(2026, 10, 29, 12, 0)
        with mock.patch("reminders.datetime", _At):
            due = [r for r in reminders._due_offers_option() if r["id"] == offer["id"]]
        self.assertEqual([r["option_end_date"] for r in due], [date(2026, 10, 31)])
        texts = []
        # run_reminders_if_due sends from a thread; run it inline.
        with mock.patch("reminders._check_due", return_value=True), mock.patch("reminders._mark_checked"), \
                mock.patch("reminders.datetime", _At), \
                mock.patch("reminders.threading.Thread", side_effect=lambda target, **k: mock.Mock(start=target)):
            reminders.run_reminders_if_due(lambda to, body: texts.append(body) or True)
        self.assertTrue(any("ends 5:00 PM local time, Sat, Oct 31, 2026 (Par. 5B, not extended for weekends/holidays)" in t
                            for t in texts), texts)


if __name__ == "__main__":
    unittest.main()
