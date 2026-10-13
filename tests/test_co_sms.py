"""Texting a Colorado offer -> CBS1 draft (co_filler.py), end to end through
the real /sms webhook with Twilio mocked, plus the deadline schedule's
invariants over every closing length the parser allows.

    python3 -m unittest tests.test_co_sms
"""
import os
import re
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import co_audit
from co_filler import build_values, split_co_address


class CoDeadlines(unittest.TestCase):
    def test_every_schedule_passes_the_checker(self):
        """Financed and cash, every close length 7..120, every weekday start:
        no out-of-order chain, nothing after closing, no weekend deadline."""
        for start in list(range(7)) + [30, 41, 45, 72, 245]:  # + runs into Cabrini/Columbus/Veterans/Thanksgiving/Juneteenth
            today = date(2026, 10, 12) + timedelta(days=start)
            for days in range(7, 121):
                for down in (0.2, 1.0):
                    parsed = {"price": 500000, "loan_amount": int(500000 * (1 - down)), "earnest_money": 5000,
                              "close_days": days, "address": "123 Main St, Denver, CO 80202",
                              "inspection_days": 10 if days > 12 else None}
                    values = build_values(parsed, today)
                    keys = [i["key"] for i in co_audit.audit_values(values)]
                    bad = [k for k in keys if k.startswith(("co_order_", "co_after_closing_", "co_price"))
                           or k in ("co_closing_weekend", "co_closing_holiday", "co_acceptance_after_closing", "co_closing_blank")]
                    self.assertEqual(bad, [], (today, days, down))
                    for k, v in values.items():
                        d = co_audit._parse_date(v) if isinstance(v, str) and k != "Date" else None
                        if d:
                            self.assertTrue(co_audit.is_business_day(d), (k, v, today, days))

    def test_cash_deal_has_no_loan_terms(self):
        v = build_values({"price": 400000, "loan_amount": 0, "earnest_money": 4000, "close_days": 21,
                          "address": "1 Elm St, Boulder, CO"}, date(2026, 10, 12))
        self.assertNotIn("New Loan", v)
        self.assertEqual(v["New Loan Availability Deadline"], "N/A")
        self.assertNotIn("Conventional Checkbox", v)
        self.assertEqual(v["Cash At Closing"], "396,000.00")


class CoAddress(unittest.TestCase):
    def test_split(self):
        self.assertEqual(split_co_address("123 Main St, Denver, Co 80202"),
                         {"street": "123 Main St", "city": "Denver", "county": "Denver", "zip": "80202"})
        self.assertEqual(split_co_address("456 Oak Ave, Colorado Springs, CO 80903")["county"], "El Paso")
        a = split_co_address("9 Pine Dr Aurora Colorado Arapahoe county")
        self.assertEqual((a["street"], a["city"], a["county"]), ("9 Pine Dr", "Aurora", "Arapahoe"))
        self.assertEqual(split_co_address("9 Pine Dr, Aurora, CO")["county"], "")  # multi-county city: not guessed


class CoSmsFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        app_module.app.config["TESTING"] = True
        cls.app = app_module
        cls.client = app_module.app.test_client()

    def _sms(self, phone, *bodies):
        sent = []
        with mock.patch("app.twilio_send_sms", side_effect=lambda to, body: sent.append(body)):
            for b in bodies:
                self.assertEqual(self.client.post("/sms", data={"From": phone, "Body": b}).status_code, 200)
        return sent

    def test_text_to_cbs1(self):
        sent = self._sms("+15125559001", "500k 20% 30day 123 Main St, Denver, CO 80202", "YES")
        confirm, done = sent[0], sent[-1]
        self.assertIn("Colorado", confirm)
        self.assertIn("Denver County", confirm)
        self.assertNotIn("option fee", confirm.lower())
        self.assertNotIn("TREC", confirm)
        confirm.encode("ascii")  # GSM-7-safe: one non-GSM char triples SMS cost
        self.assertIn("Colorado CBS1 draft", done)
        path = re.search(r"http://localhost(/review/\S+)", done).group(1)
        filename = path.split("/review/")[1].split("?")[0]
        self.assertTrue(filename.startswith("CBS1_"))

        from tc_audit import check_tc_file
        from pdf_validator import validate_offer_pdf
        from pdf_filler import OUTPUT_DIR  # set by whichever test module imported the app first
        pdf = os.path.join(OUTPUT_DIR, filename)
        r = check_tc_file([pdf])
        self.assertEqual(r["form"], "Colorado CBS1")
        blockers = sorted(i["key"] for i in r["issues"] if i["severity"] == "blocker")
        self.assertEqual(blockers, ["co_earnest_holder_blank"])  # the agent's to fill, like TX's escrow agent
        self.assertFalse(validate_offer_pdf(pdf, {})["ok"])

        page = self.client.get(path)
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("Colorado CBS1 Contract to Buy and Sell", html)
        self.assertNotIn("Not affiliated with TREC", html)

    def test_amend_makes_colorado_amend_extend(self):
        phone = "+15125559003"
        self._sms(phone, "500k 20% 30day 77 Birch Ln, Boulder, CO 80302", "YES")
        from pdf_filler import OUTPUT_DIR
        before = set(os.listdir(OUTPUT_DIR))
        sent = self._sms(phone, "AMEND 77 Birch Ln close +7")
        self.assertIn("Amend/Extend (Colorado)", sent[-1])
        self.assertNotIn("39-11", sent[-1])
        new = set(os.listdir(OUTPUT_DIR)) - before
        self.assertEqual(len(new), 1)
        name = new.pop()
        self.assertTrue(name.startswith("AE41_"), name)
        # The amendment is a real AE41 that co_forms reads back: new Closing
        # Date on a business day, after the original.
        import co_audit, co_forms
        from pdf_validator import _read_values
        v = _read_values(os.path.join(OUTPUT_DIR, name))
        self.assertEqual(co_forms.classify(v), "ae41")
        changes, conflicts = co_forms.amendment_changes(v, "ae41")
        new_close = co_audit._parse_date(changes["Closing Date"])
        self.assertTrue(co_audit.is_business_day(new_close))
        self.assertEqual(conflicts, [])
        self.assertEqual(v.get("Street Address"), "77 Birch Ln")

    def test_buyer_agreement_by_text(self):
        from pdf_filler import OUTPUT_DIR
        before = set(os.listdir(OUTPUT_DIR))
        sent = self._sms("+15125559004", "BUYER AGREEMENT Pat Buyer 2.5% 90 days agency")
        self.assertIn("Exclusive Right-to-Buy for Pat Buyer", sent[-1])
        self.assertNotIn("\u00a7", sent[-1])   # GSM-7 only
        new = [n for n in set(os.listdir(OUTPUT_DIR)) - before if n.startswith("BC60_")]
        self.assertEqual(len(new), 1)
        bad = self._sms("+15125559004", "BUYER AGREEMENT Pat Buyer 90 days")
        self.assertIn("%", bad[-1])

    def test_co_confirmation_mentions_hb26(self):
        sent = self._sms("+15125559005", "500k 20% 30day 123 Main St, Denver, CO 80202")
        self.assertIn("HB26-1426", sent[0])
        self.assertIn("BUYER AGREEMENT", sent[0])
        sent[0].encode("ascii")

    def test_other_states_still_blocked(self):
        sent = self._sms("+15125559002", "500k 20% 30day 123 Main St, Phoenix, AZ 85001")
        self.assertIn("supports Texas and Colorado", sent[0])
        self.assertIn("Arizona", sent[0].replace("AZ", "Arizona"))


if __name__ == "__main__":
    unittest.main()
