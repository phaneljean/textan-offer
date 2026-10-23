"""SMS nurture (nurture.py): each step fires once at the right time, only
with SMS_NURTURE_ENABLED on, never to subscribers / roster agents / internal
phones / opted-out or invalid numbers, and never outside 9am-8pm Central
(Sunday from noon). Twilio is mocked throughout.

    python3 -m unittest tests.test_nurture
"""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import app as app_module  # noqa: E402
import nurture  # noqa: E402
import subscriptions  # noqa: E402

# Tue 2026-10-20 15:00 UTC = 10:00 AM CDT, inside the window.
T0 = datetime(2026, 10, 20, 15, 0)
PHONE = "+15125550123"
ON = {"SMS_NURTURE_ENABLED": "1", "SMS_NURTURE_PAYWALL_ENABLED": "1"}


def _db():
    return sqlite3.connect(nurture.DB_PATH)


def _user(phone=PHONE, count=1, subscribed=0, brokerage_id=None):
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO users (phone, offer_count, is_subscribed, brokerage_id, created_at, updated_at) "
                  "VALUES (?, ?, ?, ?, ?, ?)", (phone, count, subscribed, brokerage_id, T0.isoformat(), T0.isoformat()))


def _offer(phone=PHONE, at=T0, filename="TREC_x.pdf"):
    with _db() as c:
        c.execute("INSERT INTO offers (phone, address, filename, created_at) VALUES (?, ?, ?, ?)",
                  (phone, "123 Main St", filename, at.isoformat()))


def _event(etype, phone=PHONE, at=T0, meta=None):
    with _db() as c:
        c.execute("INSERT INTO events (event_type, phone, metadata, created_at) VALUES (?, ?, ?, ?)",
                  (etype, phone, json.dumps(meta) if meta else None, at.isoformat()))


class _Base(unittest.TestCase):
    def setUp(self):
        self.assertEqual(nurture.DB_PATH, subscriptions.DB_PATH)
        with _db() as c:
            for t in ("users", "offers", "events", "sms_nurture", "sms_opt_outs"):
                c.execute(f"DELETE FROM {t}")
        self.sent = []

    def send(self, to, body):
        self.sent.append((to, body))
        return True

    def run_at(self, when, send=None):
        with mock.patch.dict(os.environ, ON):
            return nurture.run_nurture(send or self.send, now_utc=when)


class Flag(_Base):
    def test_off_by_default_sends_nothing(self):
        _user(); _offer()
        os.environ.pop("SMS_NURTURE_ENABLED", None)
        self.assertEqual(nurture.run_nurture(self.send, now_utc=T0 + timedelta(hours=25)), [])
        self.assertEqual(self.sent, [])
        with mock.patch("nurture.threading.Thread") as th:
            nurture.run_nurture_if_due(self.send)
            th.assert_not_called()

    def test_any_request_ticks_the_scheduler(self):
        with mock.patch("nurture.run_nurture_if_due") as tick:
            app_module.app.test_client().get("/")
            tick.assert_called_once_with(app_module.twilio_send_sms)


class OfferSteps(_Base):
    def test_offer1_fires_once_after_24h(self):
        _user(count=1); _offer(at=T0)
        self.assertEqual(self.run_at(T0 + timedelta(hours=23)), [])
        self.assertEqual(self.run_at(T0 + timedelta(hours=24, minutes=5)), [(PHONE, "offer1")])
        self.assertIn("Text DASHBOARD", self.sent[0][1])
        self.assertIn("Text PROFILE to save your earnest % and option fee", self.sent[0][1])
        self.assertEqual(self.run_at(T0 + timedelta(hours=25)), [])
        self.assertEqual(len(self.sent), 1)

    def test_second_offer_cancels_offer1_then_offer2_fires(self):
        _user(count=1); _offer(at=T0)
        _user(count=2); _offer(at=T0 + timedelta(hours=5))
        self.assertEqual(self.run_at(T0 + timedelta(hours=24, minutes=5)), [])  # 2nd offer is only 19h old
        self.assertEqual(self.run_at(T0 + timedelta(hours=29, minutes=5)), [(PHONE, "offer2")])
        self.assertTrue(self.sent[0][1].startswith("TxtAnOffer: 1 free offer left."))
        self.assertNotIn("offer1", [s for _, s in self.run_at(T0 + timedelta(hours=30))])

    def test_third_offer_cancels_offer2(self):
        _user(count=3); _offer(at=T0); _offer(at=T0 + timedelta(hours=1)); _offer(at=T0 + timedelta(hours=2))
        self.assertEqual(self.run_at(T0 + timedelta(hours=26)), [])

    def test_stale_steps_never_go_out(self):
        # Switching the flag on must not text people about old offers.
        _user(count=1); _offer(at=T0 - timedelta(days=30))
        self.assertEqual(self.run_at(T0), [])

    def test_colorado_copy_has_no_option_fee(self):
        _user(count=1); _offer(at=T0, filename="CBS1_x.pdf")
        self.run_at(T0 + timedelta(hours=24, minutes=5))
        self.assertNotIn("option", self.sent[0][1])

    def test_nurture_sent_event_has_step(self):
        _user(count=1); _offer(at=T0)
        self.run_at(T0 + timedelta(hours=24, minutes=5))
        with _db() as c:
            rows = c.execute("SELECT phone, metadata FROM events WHERE event_type = 'nurture_sent'").fetchall()
        self.assertEqual(rows, [(PHONE, json.dumps({"step": "offer1"}))])


class PaywallStep(_Base):
    def test_one_reminder_three_days_later_ever(self):
        _user(count=3); _event("limit_reached", at=T0)
        self.assertEqual(self.run_at(T0 + timedelta(days=2, hours=23)), [])
        self.assertEqual(self.run_at(T0 + timedelta(days=3, minutes=5)), [(PHONE, "paywall")])
        self.assertIn("$40/mo, cancel anytime: txtanoffer.com/pricing", self.sent[0][1])
        # Hits the wall again a week later: still no second reminder.
        _event("limit_reached", at=T0 + timedelta(days=7))
        self.assertEqual(self.run_at(T0 + timedelta(days=10, minutes=5)), [])
        self.assertEqual(len(self.sent), 1)

    def test_needs_its_own_flag(self):
        _user(count=3); _event("limit_reached", at=T0)
        with mock.patch.dict(os.environ, {"SMS_NURTURE_ENABLED": "1", "SMS_NURTURE_PAYWALL_ENABLED": ""}):
            self.assertEqual(nurture.run_nurture(self.send, now_utc=T0 + timedelta(days=3, minutes=5)), [])
        with mock.patch.dict(os.environ, {"SMS_NURTURE_ENABLED": "", "SMS_NURTURE_PAYWALL_ENABLED": "1"}):
            self.assertEqual(nurture.run_nurture(self.send, now_utc=T0 + timedelta(days=3, minutes=5)), [])
        self.assertEqual(self.sent, [])

    def test_subscribed_before_reminder_gets_nothing(self):
        _user(count=3, subscribed=1); _event("limit_reached", at=T0)
        self.assertEqual(self.run_at(T0 + timedelta(days=3, minutes=5)), [])


class Skips(_Base):
    def _due_offer1(self, phone=PHONE, **kw):
        _user(phone=phone, count=1, **kw); _offer(phone=phone, at=T0)
        return self.run_at(T0 + timedelta(hours=24, minutes=5))

    def test_subscriber(self):
        self.assertEqual(self._due_offer1(subscribed=1), [])

    def test_brokerage_roster(self):
        self.assertEqual(self._due_offer1(brokerage_id=7), [])

    def test_internal_phone(self):
        with mock.patch.object(subscriptions, "ADMIN_PHONES", {PHONE}):
            self.assertEqual(self._due_offer1(), [])

    def test_invalid_numbers(self):
        for phone in ("landing-demo", "+4420794600000", "+1512555012", "+10125550123"):
            with self.subTest(phone=phone):
                self.assertEqual(self._due_offer1(phone=phone), [])

    def test_stop_via_webhook_then_start(self):
        client = app_module.app.test_client()
        with mock.patch("app.twilio_send_sms") as tw:
            self.assertEqual(client.post("/sms", data={"From": PHONE, "Body": "STOP"}).status_code, 200)
            tw.assert_not_called()  # Twilio sends its own STOP confirmation
        self.assertEqual(self._due_offer1(), [])
        with mock.patch("app.twilio_send_sms"):
            client.post("/sms", data={"From": PHONE, "Body": "start"})
        self.assertEqual(self.run_at(T0 + timedelta(hours=25)), [(PHONE, "offer1")])

    def test_twilio_opt_out_type_param(self):
        with mock.patch("app.twilio_send_sms"):
            app_module.app.test_client().post("/sms", data={"From": PHONE, "Body": "cancel", "OptOutType": "STOP"})
        self.assertEqual(self._due_offer1(), [])

    def test_stop_from_before_this_table_existed(self):
        _event("sms_received", at=T0 - timedelta(days=5), meta={"body": "Stop"})
        self.assertEqual(self._due_offer1(), [])

    def test_twilio_refused_earlier_send(self):
        _event("sms_send_failed", at=T0 - timedelta(days=1), meta={"error": "HTTP 400 error: Unable to create record: Attempt to send to unsubscribed recipient (21610)"})
        self.assertEqual(self._due_offer1(), [])

    def test_skip_is_not_used_up(self):
        # A skip doesn't claim the step: STOP then START still gets the tip.
        nurture.record_opt_keyword(PHONE, "STOP")
        self.assertEqual(self._due_offer1(), [])
        nurture.record_opt_keyword(PHONE, "START")
        self.assertEqual(len(self.run_at(T0 + timedelta(hours=26))), 1)


class QuietHours(_Base):
    def test_held_overnight_then_sent_at_9am(self):
        # Offer at 8:30 PM CDT Tue -> due 8:30 PM Wed -> held until 9 AM Thu.
        at = datetime(2026, 10, 21, 1, 30)
        _user(count=1); _offer(at=at)
        self.assertEqual(self.run_at(at + timedelta(hours=24, minutes=5)), [])
        self.assertEqual(self.run_at(datetime(2026, 10, 22, 13, 55)), [])  # 8:55 AM CDT
        self.assertEqual(self.run_at(datetime(2026, 10, 22, 14, 5)), [(PHONE, "offer1")])

    def test_sunday_waits_until_noon(self):
        _user(count=1); _offer(at=datetime(2026, 10, 24, 14, 0))  # Sat 9 AM CDT
        self.assertEqual(self.run_at(datetime(2026, 10, 25, 15, 0)), [])  # Sun 10 AM
        self.assertEqual(self.run_at(datetime(2026, 10, 25, 17, 5)), [(PHONE, "offer1")])  # Sun 12:05 PM

    def test_mountain_area_code_starts_an_hour_later(self):
        phone = "+17205550123"  # Denver
        _user(phone=phone, count=1); _offer(phone=phone, at=datetime(2026, 10, 20, 14, 0))
        self.assertEqual(self.run_at(datetime(2026, 10, 21, 14, 30)), [])  # 9:30 CT = 8:30 MT
        self.assertEqual(self.run_at(datetime(2026, 10, 21, 15, 5)), [(phone, "offer1")])

    def test_window_after_dst_ends(self):
        # Nov 2: CST (UTC-6). 14:30 UTC = 8:30 AM CST: held.
        self.assertFalse(nurture.in_send_window(PHONE, datetime(2026, 11, 3, 14, 30)))
        self.assertTrue(nurture.in_send_window(PHONE, datetime(2026, 11, 3, 15, 5)))
        self.assertFalse(nurture.in_send_window(PHONE, datetime(2026, 11, 4, 2, 0)))  # 8 PM CST


class FailuresAndCopy(_Base):
    def test_failed_send_is_not_retried(self):
        _user(count=1); _offer(at=T0)
        self.assertEqual(self.run_at(T0 + timedelta(hours=24, minutes=5), send=lambda to, b: False), [])
        self.assertEqual(self.run_at(T0 + timedelta(hours=25)), [])
        with _db() as c:
            self.assertEqual(c.execute("SELECT status FROM sms_nurture").fetchall(), [("failed",)])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM events WHERE event_type='nurture_sent'").fetchone(), (0,))

    def test_concurrent_claim_sends_once(self):
        _user(count=1); _offer(at=T0)
        now = T0 + timedelta(hours=24, minutes=5)
        self.assertTrue(nurture._claim(PHONE, "offer1", T0, now))
        self.assertFalse(nurture._claim(PHONE, "offer1", T0, now))

    def test_every_message_rules(self):
        for step in ("offer1", "offer2", "paywall"):
            for co in (False, True):
                msg = nurture.message_for(step, co)
                with self.subTest(step=step, co=co):
                    self.assertTrue(msg.endswith("Reply STOP to opt out."))
                    self.assertLessEqual(len(msg), 300)
                    self.assertTrue(msg.isascii())  # GSM-7 safe
                    self.assertNotRegex(msg.lower(), r"\bai\b|minutes|hours|save time|faster")


class EndToEnd(_Base):
    def test_real_sms_offer_then_tip(self):
        client = app_module.app.test_client()
        with mock.patch("app.twilio_send_sms"):
            client.post("/sms", data={"From": PHONE, "Body": "725k 3% 21day 123 Main St, Austin, TX Travis"})
            client.post("/sms", data={"From": PHONE, "Body": "YES"})
        self.assertEqual(subscriptions.get_user(PHONE)["offer_count"], 1)
        with _db() as c:
            created = datetime.fromisoformat(c.execute("SELECT created_at FROM offers WHERE phone=?", (PHONE,)).fetchone()[0])
        when = created + timedelta(hours=24, minutes=1)
        while not nurture.in_send_window(PHONE, when):
            when += timedelta(minutes=30)
        self.assertEqual(self.run_at(when), [(PHONE, "offer1")])


if __name__ == "__main__":
    unittest.main()
