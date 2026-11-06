"""Deadline Autopilot (free tier): tc_deadlines.compute_deadlines /
build_ics, tc_audit's key_dates extraction, and the report email's
deadline section + .ics attachment.

    python3 -m unittest tests.test_tc_deadlines
"""
import base64
import os
import unittest
from datetime import date

import tc_deadlines
import tc_check_email
from tc_audit import check_tc_file

HERE = os.path.dirname(os.path.abspath(__file__))
CLEAN = os.path.join(HERE, "fixtures", "trec_20-19_clean_two_parties.pdf")
CLEAN_FA = os.path.join(HERE, "fixtures", "trec_40-11_clean.pdf")

DEMO = {"effective_date": "2026-11-10", "closing_date": "2026-12-10",
        "option_days": 7, "disclosure_days": None,
        "earnest_money": 4250.0, "option_fee": 250.0}


def by_key(items):
    return {i["key"]: i for i in items}


class ComputeDeadlines(unittest.TestCase):
    def test_demo_timeline(self):
        items = by_key(tc_deadlines.compute_deadlines(DEMO))
        self.assertEqual(items["earnest_money"]["date"], date(2026, 11, 13))
        self.assertEqual(items["option_end"]["date"], date(2026, 11, 17))
        self.assertEqual(items["option_end"]["time_label"], "5:00 PM")
        self.assertEqual(items["closing"]["date"], date(2026, 12, 10))
        self.assertNotIn("disclosure", items)  # no 7B(2) days -> no entry at all

    def test_earnest_money_extends_past_weekend_and_tx_holiday(self):
        # Effective Wed Nov 25, 2026 -> +3 lands Sat Nov 28; 5A(2) extends
        # to Mon Nov 30 (Fri Nov 27 is the Friday after Thanksgiving, a
        # Texas Legal Holiday, but the count lands after it).
        kd = dict(DEMO, effective_date="2026-11-25", option_days=None, option_fee=None)
        items = by_key(tc_deadlines.compute_deadlines(kd))
        self.assertEqual(items["earnest_money"]["date"], date(2026, 11, 30))

    def test_option_period_never_extends(self):
        # Effective Wed Nov 4, 2026 + 3 days = Sat Nov 7: stays Saturday.
        kd = dict(DEMO, effective_date="2026-11-04", option_days=3)
        items = by_key(tc_deadlines.compute_deadlines(kd))
        self.assertEqual(items["option_end"]["date"], date(2026, 11, 7))

    def test_disclosure_days_calendar_count(self):
        kd = dict(DEMO, disclosure_days=5)
        items = by_key(tc_deadlines.compute_deadlines(kd))
        self.assertEqual(items["disclosure"]["date"], date(2026, 11, 15))

    def test_blank_option_days_with_fee_is_pending_not_invented(self):
        kd = dict(DEMO, option_days=None)
        items = by_key(tc_deadlines.compute_deadlines(kd))
        self.assertIsNone(items["option_end"]["date"])
        self.assertIn("blank in Par. 5B", items["option_end"]["pending_reason"])

    def test_no_effective_date_no_deadlines(self):
        self.assertEqual(tc_deadlines.compute_deadlines({}), [])
        self.assertEqual(tc_deadlines.compute_deadlines({"effective_date": None}), [])
        self.assertEqual(tc_deadlines.compute_deadlines(None), [])

    def test_sorted_earliest_first(self):
        dates = [i["date"] for i in tc_deadlines.compute_deadlines(DEMO) if i["date"]]
        self.assertEqual(dates, sorted(dates))


class BuildIcs(unittest.TestCase):
    def setUp(self):
        self.ics = tc_deadlines.build_ics(tc_deadlines.compute_deadlines(DEMO),
                                          "123 Main St, Austin, TX 78704")

    def test_one_event_per_dated_deadline(self):
        self.assertEqual(self.ics.count("BEGIN:VEVENT"), 3)
        self.assertIn("DTSTART;VALUE=DATE:20261113", self.ics)
        self.assertIn("DTSTART;VALUE=DATE:20261210", self.ics)

    def test_option_end_is_timed_5pm_central_with_two_alarms(self):
        self.assertIn("DTSTART;TZID=America/Chicago:20261117T170000", self.ics)
        option_block = self.ics.split("UID:option_end")[1].split("END:VEVENT")[0]
        self.assertIn("TRIGGER:-P1D", option_block)
        self.assertIn("TRIGGER:-PT3H", option_block)

    def test_no_line_over_75_octets(self):
        for line in self.ics.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75, line)

    def test_pending_entries_never_reach_the_calendar(self):
        kd = dict(DEMO, option_days=None)  # fee present, days blank -> pending
        ics = tc_deadlines.build_ics(tc_deadlines.compute_deadlines(kd), "x")
        self.assertNotIn("option_end", ics)


class KeyDatesExtraction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = check_tc_file([CLEAN, CLEAN_FA])

    def test_clean_fixture_key_dates(self):
        kd = self.r["key_dates"]
        self.assertEqual(kd["effective_date"], "2026-10-08")
        self.assertEqual(kd["closing_date"], "2026-11-30")
        self.assertEqual(kd["option_days"], 10)
        self.assertEqual(kd["disclosure_days"], 5)
        self.assertEqual(kd["earnest_money"], 5000.0)
        self.assertEqual(kd["option_fee"], 250.0)

    def test_clean_fixture_deadlines(self):
        items = by_key(tc_deadlines.compute_deadlines(self.r["key_dates"]))
        # Oct 8 + 3 = Sun Oct 11 -> 5A(2) extends to Mon Oct 12 (Columbus
        # Day is federal, not a Texas Legal Holiday, so it does not extend).
        self.assertEqual(items["earnest_money"]["date"], date(2026, 10, 12))
        self.assertEqual(items["option_end"]["date"], date(2026, 10, 18))
        self.assertEqual(items["disclosure"]["date"], date(2026, 10, 13))
        self.assertEqual(items["closing"]["date"], date(2026, 11, 30))


class EmailWiring(unittest.TestCase):
    def _result(self, key_dates=DEMO):
        return {"recognized": True, "complete": False, "issues": [],
                "key_dates": key_dates,
                "property": {"address": "123 Main St", "city": "Austin", "county": "Travis"},
                "severity": {"level": "clean", "emoji": "", "label": "Ready", "issue_count": 0},
                "page_count": 12}

    def test_text_body_has_deadline_section(self):
        body = tc_check_email.format_reply_body(self._result())
        self.assertIn("DEADLINE AUTOPILOT", body)
        self.assertIn("Fri, Nov 13, 2026", body)
        self.assertIn("Tue, Nov 17, 2026, 5:00 PM", body)
        self.assertIn("txtanoffer-deadlines.ics", body)

    def test_html_has_deadline_panel(self):
        html = tc_check_email.format_reply_html(self._result())
        self.assertIn("Deadline Autopilot", html)
        self.assertIn("Thu, Dec 10, 2026", html)

    def test_attachment_is_text_calendar(self):
        att = tc_check_email.deadline_attachment(self._result())
        self.assertEqual(len(att), 1)
        self.assertEqual(att[0]["filename"], "txtanoffer-deadlines.ics")
        self.assertEqual(att[0]["type"], "text/calendar")
        self.assertTrue(base64.b64decode(att[0]["content"]).decode().startswith("BEGIN:VCALENDAR"))

    def test_no_key_dates_no_section_no_attachment(self):
        result = self._result(key_dates={})
        self.assertNotIn("DEADLINE AUTOPILOT", tc_check_email.format_reply_body(result))
        self.assertEqual(tc_check_email.deadline_attachment(result), [])


class WebSurface(unittest.TestCase):
    """The calendar also lives on the web report: the payload carries the
    sheet and POST /tc-check/deadlines.ics rebuilds the file server-side
    from key_dates alone (never trusting a client-supplied date)."""

    @classmethod
    def setUpClass(cls):
        import app
        cls.client = app.app.test_client()

    def test_ics_endpoint_builds_from_key_dates(self):
        r = self.client.post("/tc-check/deadlines.ics",
                             json={"key_dates": DEMO, "property": {"address": "123 Main St"}})
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/calendar", r.headers["Content-Type"])
        body = r.get_data(as_text=True)
        self.assertEqual(body.count("BEGIN:VEVENT"), 3)
        self.assertIn("DTSTART;TZID=America/Chicago:20261117T170000", body)

    def test_ics_endpoint_rejects_uncomputable(self):
        r = self.client.post("/tc-check/deadlines.ics",
                             json={"key_dates": {"effective_date": "not-a-date"}})
        self.assertEqual(r.status_code, 400)

    def test_ics_endpoint_sanitizes_out_of_range_days(self):
        r = self.client.post("/tc-check/deadlines.ics",
                             json={"key_dates": dict(DEMO, option_days=99999)})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("option_end", r.get_data(as_text=True))

    def test_check_page_carries_the_renderer(self):
        html = self.client.get("/tc-check").get_data(as_text=True)
        self.assertIn("buildDeadlines", html)
        self.assertIn("/tc-check/deadlines.ics", html)


if __name__ == "__main__":
    unittest.main()
