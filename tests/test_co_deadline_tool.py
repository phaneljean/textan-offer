"""Free CBS1 deadline calculator (/tools/colorado-deadlines + the embed
widget, 2026-10-13): the date math follows the CBS1's own §3.2/§3.3 text,
the routes render, only the widget may be framed by other sites, the .ics
is well-formed, and every new click tag reaches /v1/evt.

    python3 -m unittest tests.test_co_deadline_tool
"""
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import date

_TMP = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_PATH", os.path.join(_TMP, "test.db"))
os.environ.setdefault("OFFER_OUTPUT_DIR", os.path.join(_TMP, "offers"))
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)

import analytics
import app as app_module
import co_audit
import co_deadline_tool as cdt

UA = {"User-Agent": "Mozilla/5.0 (Macintosh) test"}


def _row(result, key):
    return next(r for r in result["rows"] if r["key"] == key)


def _texts(row):
    return " | ".join(f["text"] for f in row["flags"])


class DayCounting(unittest.TestCase):
    def test_days_after_mec_are_calendar_days_first_day_excluded(self):
        # §3.3.2: first day excluded, last included -> MEC + 10 = 10 days later.
        r = cdt.compute(date(2026, 10, 13), {"record_title": "10", "loan_app": "MEC + 3", "spd": "7 days"})
        self.assertEqual(_row(r, "record_title")["effective"], "2026-10-23")
        self.assertEqual(_row(r, "loan_app")["effective"], "2026-10-16")
        self.assertEqual(_row(r, "spd")["effective"], "2026-10-20")
        self.assertEqual(_row(r, "record_title")["flags"], [])

    def test_fixed_dates_in_the_formats_co_audit_reads(self):
        r = cdt.compute(date(2026, 10, 13), {"closing": "11/20/2026", "possession": "2026-11-20", "appraisal": "Nov 2, 2026"})
        self.assertEqual([_row(r, k)["effective"] for k in ("closing", "possession", "appraisal")],
                         ["2026-11-20", "2026-11-20", "2026-11-02"])

    def test_unreadable_entry_is_flagged_not_guessed(self):
        r = cdt.compute(date(2026, 10, 13), {"spd": "next week", "ilc": "999"})
        for k in ("spd", "ilc"):
            self.assertEqual(_row(r, k)["kind"], "invalid")
            self.assertIsNone(_row(r, k)["effective"])
            self.assertIn("Couldn’t read", _texts(_row(r, k)))


class WeekendsAndHolidays(unittest.TestCase):
    # MEC Sat 2026-09-26: +7 = Sat 10/03, +9 = Mon 10/05 (Cabrini Day),
    # +16 = Mon 10/12 (Columbus Day, federal), +8 = Sun 10/04.
    E = {"record_title": "7", "insp_obj": "9", "appraisal": "16", "spd": "8"}
    MEC = date(2026, 9, 26)

    def test_neither_box_checked_means_not_extended(self):
        r = cdt.compute(self.MEC, self.E, "")
        row = _row(r, "record_title")
        self.assertEqual((row["date"], row["effective"]), ("2026-10-03", "2026-10-03"))
        self.assertIn("Falls on a Saturday. Neither §3.3.3 box is checked, so the deadline is not extended.", _texts(row))

    def test_will_not_is_not_extended(self):
        row = _row(cdt.compute(self.MEC, self.E, "will_not"), "spd")
        self.assertEqual(row["effective"], "2026-10-04")
        self.assertIn("Falls on a Sunday. §3.3.3 “Will Not” is checked", _texts(row))

    def test_cabrini_day_is_a_colorado_holiday(self):
        self.assertEqual(co_audit.holiday_name(date(2026, 10, 5)), "Cabrini Day (Colorado)")
        row = _row(cdt.compute(self.MEC, self.E, ""), "insp_obj")
        self.assertEqual(row["effective"], "2026-10-05")
        self.assertIn("Falls on Cabrini Day (Colorado).", _texts(row))

    def test_will_extends_past_weekend_and_holiday_chain(self):
        r = cdt.compute(self.MEC, self.E, "will")
        # Sat 10/03 -> Sun -> Mon Cabrini Day -> Tue 10/06.
        self.assertEqual(_row(r, "record_title")["effective"], "2026-10-06")
        self.assertEqual(_row(r, "insp_obj")["effective"], "2026-10-06")
        self.assertEqual(_row(r, "appraisal")["effective"], "2026-10-13")   # past Columbus Day
        self.assertIn("extended to Tue, Oct 6, 2026", _texts(_row(r, "record_title")))

    def test_closing_on_weekend_or_holiday_is_flagged_never_moved(self):
        r = cdt.compute(date(2026, 10, 13), {"closing": "11/26/2026", "possession": "11/28/2026"}, "will")
        c, p = _row(r, "closing"), _row(r, "possession")
        self.assertEqual(c["effective"], "2026-11-26")
        self.assertIn("Falls on Thanksgiving. Banks may not move wires", _texts(c))
        self.assertEqual(p["effective"], "2026-11-28")
        self.assertIn("Falls on a Saturday. Confirm.", _texts(p))


class AfterClosingOrderAndNA(unittest.TestCase):
    def test_after_closing(self):
        r = cdt.compute(date(2026, 10, 13), {"closing": "11/20/2026", "appraisal": "12/01/2026", "possession": "11/23/2026"})
        self.assertIn("After the Closing Date (Fri, Nov 20, 2026).", _texts(_row(r, "appraisal")))
        self.assertNotIn("After the Closing", _texts(_row(r, "possession")))   # possession may follow closing

    def test_extension_can_push_past_closing(self):
        # Fri 11/20 closing; Sat 11/21 deadline extends to Mon 11/23.
        r = cdt.compute(date(2026, 10, 13), {"closing": "11/20/2026", "loan_avail": "11/21/2026"}, "will")
        self.assertIn("After the Closing Date", _texts(_row(r, "loan_avail")))

    def test_out_of_order_uses_written_dates(self):
        r = cdt.compute(date(2026, 9, 26), {"insp_obj": "10/05/2026", "insp_res": "10/03/2026"}, "will")
        self.assertIn("Out of order: before Inspection Objection Deadline (item 31", _texts(_row(r, "insp_res")))
        r = cdt.compute(date(2026, 10, 13), {"record_title": "20", "record_title_obj": "10"})
        self.assertIn("Out of order", _texts(_row(r, "record_title_obj")))

    def test_na_blank_deleted_rows_carry_the_3_2_note(self):
        r = cdt.compute(date(2026, 10, 13), {"loan_avail": "N/A", "appraisal": "Deleted", "insp_obj": ""})
        for k in ("loan_avail", "appraisal", "insp_obj"):
            row = _row(r, k)
            self.assertEqual(row["kind"], "deleted")
            self.assertIn("Under §3.2 this deadline is not applicable and the provision containing it is deleted.", _texts(row))
        self.assertIn(15, r["deleted_items"])
        self.assertIn("Blank, N/A or Deleted (§3.2", r["text"])

    def test_before_mec_and_possession_before_closing(self):
        r = cdt.compute(date(2026, 10, 13), {"spd": "10/01/2026", "closing": "11/20/2026", "possession": "11/18/2026"})
        self.assertIn("Before MEC", _texts(_row(r, "spd")))
        self.assertIn("Early possession", _texts(_row(r, "possession")))

    def test_no_closing_note(self):
        self.assertIn("No Closing Date entered", " ".join(cdt.compute(date(2026, 10, 13), {"spd": "5"})["notes"]))

    def test_rows_match_the_form_labels_co_audit_knows(self):
        # Item numbers/labels shared with co_audit must agree (item 3 is printed
        # "Record Title Deadline (and Tax Certificate)" on the form).
        for key, item, _, _, label in cdt.ROWS:
            if key in co_audit.DEADLINES:
                self.assertEqual(co_audit.DEADLINES[key][1], item, key)
                if key != "record_title":
                    # co_audit writes a straight apostrophe; the form prints a curly one.
                    self.assertEqual(co_audit.DEADLINES[key][2], label.replace("\u2019", "'"), key)
        self.assertEqual([r[1] for r in cdt.ROWS], list(range(2, 41)))


class Ics(unittest.TestCase):
    def test_ics_is_well_formed(self):
        r = cdt.compute(date(2026, 10, 13), {"record_title": "10", "insp_obj": "Nov 2, 2026", "closing": "11/20/2026", "loan_avail": "N/A"})
        ics = cdt.to_ics(r)
        self.assertTrue(ics.endswith("\r\n"))
        self.assertNotIn("\n", ics.replace("\r\n", ""))         # CRLF only
        lines = ics.split("\r\n")[:-1]
        self.assertEqual((lines[0], lines[-1]), ("BEGIN:VCALENDAR", "END:VCALENDAR"))
        for ln in lines:
            self.assertLessEqual(len(ln.encode("utf-8")), 75, ln)
        unfolded = re.sub(r"\r\n ", "", ics).split("\r\n")
        self.assertEqual(sum(1 for l in unfolded if l == "BEGIN:VEVENT"), 3)
        self.assertEqual(sum(1 for l in unfolded if l == "END:VEVENT"), 3)
        for prop in ("VERSION:2.0", "PRODID:", "DTSTART;VALUE=DATE:20261023", "DTEND;VALUE=DATE:20261024", "DTSTAMP:"):
            self.assertTrue(any(l.startswith(prop) for l in unfolded), prop)
        self.assertEqual(len({l for l in unfolded if l.startswith("UID:")}), 3)
        self.assertTrue(any(l.startswith("SUMMARY:Closing Date (CBS1 item 39)") for l in unfolded))

    def test_ics_route(self):
        c = app_module.app.test_client()
        res = c.get("/tools/colorado-deadlines.ics?mec=2026-10-13&d_record_title=10", headers=UA)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, "text/calendar")
        self.assertIn("attachment", res.headers["Content-Disposition"])
        self.assertIn("DTSTART;VALUE=DATE:20261023", res.get_data(as_text=True))
        self.assertEqual(c.get("/tools/colorado-deadlines.ics?mec=", headers=UA).status_code, 400)


class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = app_module.app.test_client()
        cls.page = cls.c.get("/tools/colorado-deadlines", headers=UA)
        cls.embed = cls.c.get("/embed/colorado-deadlines", headers=UA)

    def test_pages_render(self):
        self.assertEqual(self.page.status_code, 200)
        self.assertEqual(self.embed.status_code, 200)
        html = self.page.get_data(as_text=True)
        self.assertIn("<title>Colorado CBS1 Deadline Calculator", html)
        self.assertIn('<body data-page="co_deadlines_page">', html)
        self.assertIn("forward the CBS1 to tc@check.txtanoffer.com", html)
        self.assertIn(cdt.SOURCE_NOTE, html)
        self.assertIn("&lt;iframe src=&quot;https://txtanoffer.com/embed/colorado-deadlines&quot;", html)

    def test_embed_is_cookie_free_and_minimal(self):
        html = self.embed.get_data(as_text=True)
        self.assertNotIn("Set-Cookie", self.embed.headers)
        self.assertNotIn('class="nav', html)
        self.assertIn('<meta name="robots" content="noindex">', html)
        self.assertIn('target="_blank" rel="noopener">Powered by TxtAnOffer &middot; Free CBS1 deadline calculator</a>', html)
        self.assertIn("?src=embed", html)
        self.assertIn(cdt.SOURCE_NOTE, html)

    def test_only_the_embed_may_be_framed(self):
        self.assertEqual(self.embed.headers.get("Content-Security-Policy"), "frame-ancestors *")
        self.assertNotIn("X-Frame-Options", self.embed.headers)
        for path in ("/", "/tools/colorado-deadlines", "/tc-check", "/tc-hub/colorado", "/v1/tools/co-deadlines?mec=2026-10-13"):
            res = self.c.get(path, headers=UA)
            with self.subTest(path=path):
                self.assertNotIn("frame-ancestors *", res.headers.get("Content-Security-Policy", ""))

    def test_sitemap_and_links(self):
        sm = self.c.get("/sitemap.xml").get_data(as_text=True)
        self.assertIn("https://txtanoffer.com/tools/colorado-deadlines</loc>", sm)
        self.assertNotIn("/embed/", sm)
        self.assertIn('href="/tools/colorado-deadlines"', self.c.get("/tc-hub/colorado", headers=UA).get_data(as_text=True))
        self.assertIn('href="/tools/colorado-deadlines" data-evt="states_co_tool"', self.c.get("/", headers=UA).get_data(as_text=True))

    def test_new_evt_names_whitelisted_and_counted(self):
        for name in ("tool_calc_run", "tool_ics", "tool_copy_text", "tool_embed_copy", "tool_check_cta", "states_co_tool", "hub_tool_link"):
            self.assertIn(name, app_module._ENGAGEMENT_TYPES)
        self.assertIn("tool_check_cta", analytics.CHECK_CTA_EVENTS)
        self.assertIn("co_deadlines_page", app_module._TRACKED_PAGES)
        for page in (self.page.get_data(as_text=True), self.embed.get_data(as_text=True)):
            for name in set(re.findall(r'data-evt="([a-z0-9_]+)"', page)):
                self.assertIn(name, app_module._ENGAGEMENT_TYPES)
        self.c.set_cookie("ta_vid", "a" * 32)
        self.assertEqual(self.c.post("/v1/evt", data={"type": "tool_check_cta", "page": "co_deadlines_page"}, headers=UA).status_code, 204)
        conn = sqlite3.connect(analytics.DB_PATH)
        got = [json.loads(m)["type"] for (m,) in conn.execute("SELECT metadata FROM events WHERE event_type = 'page_engagement'")]
        conn.close()
        self.assertIn("tool_check_cta", got)

    def test_compute_endpoint_logs_one_anonymous_event(self):
        conn = sqlite3.connect(analytics.DB_PATH)
        conn.execute("DELETE FROM events WHERE event_type IN ('tool_calc_run', 'tool_ics')")
        conn.commit()
        q = "/v1/tools/co-deadlines?mec=2026-10-13&d_record_title=10&d_spd=Jane%20Doe&surface=embed&host=example.com"
        r1 = self.c.get(q + "&log=1", headers=UA)
        self.c.get(q, headers=UA)   # recalculation: not logged again
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.headers["Cache-Control"], "no-store")
        self.assertEqual(_row(r1.get_json(), "record_title")["effective"], "2026-10-23")
        rows = conn.execute("SELECT metadata FROM events WHERE event_type = 'tool_calc_run'").fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0][0])
        self.assertEqual(meta, {"surface": "embed", "host": "example.com", "dated": 1})   # no entries kept
        usage = analytics.get_co_tool_usage(days=1)
        self.assertEqual((usage[0]["surface"], usage[0]["host"], usage[0]["runs"]), ("embed", "example.com", 1))
        self.assertEqual(self.c.get("/v1/tools/co-deadlines?mec=nope", headers=UA).status_code, 400)

    def test_calculator_js_parses(self):
        path = os.path.join(_TMP, "cdc.js")
        with open(path, "w") as f:
            f.write(cdt.CALC_JS.replace("<script>", "").replace("</script>", ""))
        try:
            subprocess.run(["node", "--check", path], check=True, capture_output=True)
        except FileNotFoundError:
            self.skipTest("node not installed")


if __name__ == "__main__":
    unittest.main()
