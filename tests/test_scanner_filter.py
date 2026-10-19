"""Email security scanners kept out of "Real visitors" on /analytics
(2026-10-19). On 10-19, 15 title cold emails went out 4:52-4:58 PM CT and
ti_*/co_ti_* hits landed on /tc-check 4:54-4:59 across nearly every
recipient, running JS and "seeing" the drop box, so "Cold email: 22 real
visitors" was mostly mail gateways. These tests pin the classifier and,
above all, that a browser that does anything past looking still counts.

    python3 -m unittest tests.test_scanner_filter
"""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import analytics
import app as app_module

# User agents seen on 10-19 (Chrome 147/148/154 are real, recent releases:
# stable was 155 that day) plus the two that really are scanner tells.
WIN_CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v}.0.0.0 Safari/537.36"
FAKE_EDGE = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/125.0.0.0 Safari/537.36 Edge/12.246")
APPENGINE = "AppEngine-Google; (+http://code.google.com/appengine; appid: s~virustotalcloud)"
MAC_CHROME = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 19_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
          "Version/19.0 Mobile/15E148 Safari/604.1")


def _vid(n):
    return f"{n:032x}"


class UserAgentAndReferrer(unittest.TestCase):
    def test_current_and_recent_browsers_are_not_scanners(self):
        for ua in (WIN_CHROME.format(v=147), WIN_CHROME.format(v=148), WIN_CHROME.format(v=154),
                   WIN_CHROME.format(v=158),   # stable + 3 (Canary range): still allowed
                   MAC_CHROME, IPHONE,
                   WIN_CHROME.format(v=152) + " Edg/152.0.0.0",
                   "Mozilla/5.0 (Windows NT 10.0; rv:158.0) Gecko/20100101 Firefox/158.0",
                   # Real legacy Edge (2015-2020) carried an old Chrome token.
                   "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/42.0.2311.135 Safari/537.36 Edge/12.246",
                   "", "(not logged)"):
            with self.subTest(ua=ua):
                self.assertEqual(analytics.scanner_ua_reason(ua), "")

    def test_made_up_versions_and_known_fetchers(self):
        stable = analytics.CURRENT_STABLE_MAJOR
        for ua in (FAKE_EDGE, APPENGINE,
                   WIN_CHROME.format(v=stable["chrome"] + analytics.FUTURE_VERSION_SLACK + 1),
                   WIN_CHROME.format(v=150) + f" Edg/{stable['edge'] + 4}.0.0.0",
                   f"Mozilla/5.0 (X11; Linux x86_64; rv:170.0) Gecko/20100101 Firefox/170.0"):
            with self.subTest(ua=ua):
                self.assertNotEqual(analytics.scanner_ua_reason(ua), "")

    def test_bumping_the_constant_moves_the_line(self):
        old = dict(analytics.CURRENT_STABLE_MAJOR)
        try:
            analytics.CURRENT_STABLE_MAJOR["chrome"] = 142
            self.assertNotEqual(analytics.scanner_ua_reason(WIN_CHROME.format(v=154)), "")
        finally:
            analytics.CURRENT_STABLE_MAJOR.update(old)

    def test_link_protection_referrers(self):
        for host in ("link.edgepilot.com", "nam12.safelinks.protection.outlook.com", "urldefense.com",
                     "urldefense.proofpoint.com", "protect-us.mimecast.com", "linkprotect.cudasvc.com",
                     "secure-web.cisco.com", "clicktime.symantec.com"):
            with self.subTest(host=host):
                self.assertTrue(analytics.scanner_referrer_reason(host))
        for host in ("", "(none)", "(internal)", "linkedin.com", "google.com", "mail.google.com",
                     "outlook.live.com", "notedgepilot.com"):
            with self.subTest(host=host):
                self.assertEqual(analytics.scanner_referrer_reason(host), "")

    def test_co_ti_is_cold_email(self):
        self.assertEqual(analytics._channel_for_source("co_ti_elevated"), "Cold email")
        self.assertEqual(analytics._channel_for_source("ti_keydfw"), "Cold email")


class Dashboard(unittest.TestCase):
    """Rows shaped like 10-19, run through every count on /analytics."""
    def setUp(self):
        conn = sqlite3.connect(analytics.DB_PATH)
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM internal_visitors")
        conn.commit()
        conn.close()
        # "4:54 PM CT", kept inside every window (7-day headline, 48h table).
        self.t0 = datetime.utcnow() - timedelta(hours=2)
        self.n = 0

    def _ev(self, event_type, meta, minutes):
        conn = sqlite3.connect(analytics.DB_PATH)
        conn.execute("INSERT INTO events (event_type, phone, metadata, created_at) VALUES (?, NULL, ?, ?)",
                     (event_type, json.dumps(meta), (self.t0 + timedelta(minutes=minutes)).isoformat()))
        conn.commit()
        conn.close()

    def _visit(self, src, minutes, ua=WIN_CHROME.format(v=148), referrer="", js=True, then=(), page="tc_check_page"):
        """One browser: a page view, the two on-load beacons a headless scanner
        also fires, then anything in `then` (beacon types, or event types
        like tc_check_attempted)."""
        self.n += 1
        v = _vid(self.n)
        self._ev("page_view", {"page": page, "visitor": v, "referrer": referrer, "source": src,
                               "device": "desktop", "ua": ua}, minutes)
        if js:
            for t in ("js_ok", "dropzone_seen"):
                self._ev("page_engagement", {"type": t, "page": page, "visitor": v, "device": "desktop"}, minutes)
        for t in then:
            if t.startswith("tc_check"):
                self._ev(t, {"visitor": v, "source": src}, minutes + 1)
            elif t.startswith("page:"):
                self._ev("page_view", {"page": t[5:], "visitor": v, "referrer": "(internal)", "source": src,
                                       "device": "desktop", "ua": ua}, minutes + 1)
            else:
                self._ev("page_engagement", {"type": t, "page": page, "visitor": v, "device": "desktop"}, minutes + 0.5)
        return v

    def _load_1019(self):
        """15 recipients opened by gateways 4:54-4:59, Elevated hit again at
        5:57 and 6:04, a few VirusTotal fetches (no JS), one EdgePilot."""
        tx = ["keydfw", "alamo", "trinity", "lonestar", "heritage", "republic", "independence", "capital", "texan", "gulf"]
        co = ["empire", "elevated", "firstalliance", "ascent", "cotitleclosing"]
        uas = [WIN_CHROME.format(v=147), WIN_CHROME.format(v=148), WIN_CHROME.format(v=154), FAKE_EDGE]
        for i, firm in enumerate(tx):
            self._visit("ti_" + firm, 2 + i * 0.5, ua=uas[i % 4], referrer="link.edgepilot.com" if i == 3 else "")
        for i, firm in enumerate(co):
            self._visit("co_ti_" + firm, 3 + i * 0.5, ua=uas[(i + 1) % 4])
        for burst in (5, 65, 72):   # 4:57, 5:57, 6:04
            for k in range(4):
                self._visit("co_ti_elevated", burst + k * 0.2, ua=uas[k])
        for i in range(3):
            self._visit("ti_" + tx[i], 2.2 + i, ua=APPENGINE, js=False)

    def test_1019_burst_is_not_real_visitors(self):
        self._load_1019()
        self.assertEqual(analytics.get_headline(days=7)["real_visitors"], 0)
        ch = {r["key"]: r for r in analytics.get_engagement_by_source(days=30)["channels"]}
        self.assertEqual(ch["Cold email"]["real"], 0)
        self.assertEqual(ch["Cold email"]["scanners"], 10 + 5 + 12 + 3)
        src = {r["key"]: r for r in analytics.get_engagement_by_source(days=30)["sources"]}
        self.assertEqual((src["co_ti_elevated"]["real"], src["co_ti_elevated"]["scanners"]), (0, 13))
        self.assertEqual(analytics.get_daily_funnel(days=1)[0]["js_ok"], 0)
        self.assertEqual(sum(r["js_ok"] for r in analytics.get_engagement_by_device(days=7)), 0)
        rv = analytics.get_recent_visitors(hours=48)
        self.assertEqual(len(rv), 30)                      # labeled, not hidden
        self.assertTrue(all(v["scanner"] for v in rv))

    def test_page_labels_scanners(self):
        self._load_1019()
        old = app_module.ANALYTICS_PASSWORD
        app_module.ANALYTICS_PASSWORD = "pw"
        try:
            html = app_module.app.test_client().get("/analytics?token=pw&src_ua=co_ti_",
                                                     headers={"User-Agent": "Mozilla/5.0 (Macintosh) test"}).get_data(as_text=True)
        finally:
            app_module.ANALYTICS_PASSWORD = old
        self.assertIn("Delivered (scanner hits)", html)
        self.assertIn("scanner <span class='dim'>(burst:", html)
        self.assertIn("0 ran the page in a real browser, 17 marked scanner", html)
        self.assertIn("30 visitors, 0 real", html)

    def test_human_who_interacts_is_never_filtered(self):
        """Inside the 10-19 burst, on every scanner signal at once (made-up
        UA, EdgePilot referrer), each kind of action still counts."""
        self._load_1019()
        actions = [("stay_10s",), ("nav_check_cta",), ("dropzone_interact",), ("tc_sample_btn",),
                   ("book_call_tccheck",), ("tc_check_attempted",), ("tc_check_demo_used",), ("page:homepage",)]
        humans = [self._visit("ti_keydfw", 3, ua=FAKE_EDGE, referrer="link.edgepilot.com", then=a) for a in actions]
        self.assertEqual(analytics.get_headline(days=7)["real_visitors"], len(humans))
        ch = {r["key"]: r for r in analytics.get_engagement_by_source(days=30)["channels"]}
        self.assertEqual(ch["Cold email"]["real"], len(humans))
        rv = {v["visitor"]: v for v in analytics.get_recent_visitors(hours=48)}
        self.assertEqual([rv[h]["scanner"] for h in humans], [""] * len(humans))

    def test_no_burst_no_flag(self):
        # Two recipients in the same minute, or three spread over 20 minutes: people.
        self._visit("ti_alamo", 0)
        self._visit("ti_keydfw", 0.5)
        self._visit("d1020_a", 0)
        self._visit("d1020_b", 10)
        self._visit("d1020_c", 20)
        self.assertEqual(analytics.get_headline(days=7)["real_visitors"], 5)

    def test_burst_rule_is_cold_email_only(self):
        # A LinkedIn post can send three real people in a minute.
        for k in range(4):
            self._visit("li_co_flyer", k * 0.2, ua=IPHONE)
        self.assertEqual(analytics.get_headline(days=7)["real_visitors"], 4)

    def test_phanel_and_his_browser_unaffected(self):
        own = self._visit("ti_keydfw", 2, ua=MAC_CHROME)
        analytics.set_internal_visitor(own, True)
        self._visit("ti_alamo", 2.1, ua=MAC_CHROME)
        self._visit("ti_trinity", 2.2, ua=MAC_CHROME)
        # Phanel's own click doesn't complete a burst for the other two.
        self.assertEqual(analytics.get_headline(days=7)["real_visitors"], 2)


if __name__ == "__main__":
    unittest.main()
