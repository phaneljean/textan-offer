"""Click tracking for every "check a file" CTA (2026-10-13). Until then the
/analytics "Clicked 'Check a file'" column only counted the homepage hero
button, so the nav button on every page read as 0 clicks. These tests keep
every check link tagged, every tag accepted by /v1/evt, and the funnel
counting any of them.

    python3 -m unittest tests.test_tracking
"""
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import analytics
import app as app_module

UA = {"User-Agent": "Mozilla/5.0 (Macintosh) test"}
PAGES = ["/", "/tc-check", "/pricing", "/trust", "/about", "/faq", "/tc-hub", "/tc-hub/colorado",
         "/guides/trec-20-19-checklist", "/brokers", "/terms", "/privacy", "/trec-changes", "/contact", "/archive", "/guides"]


def _vid(n):
    return f"{n:032x}"


def _clear_events():
    conn = sqlite3.connect(analytics.DB_PATH)
    conn.execute("DELETE FROM events")
    conn.commit()
    conn.close()


def _beacon(client, etype, page, visitor):
    # The test client drops a raw Cookie header in favor of its own jar.
    client.set_cookie("ta_vid", visitor)
    return client.post("/v1/evt", data={"type": etype, "page": page}, headers=UA)


class EveryCheckLinkIsTagged(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        client = app_module.app.test_client()
        cls.html = {p: client.get(p, headers=UA).get_data(as_text=True) for p in PAGES}

    def test_pages_carry_the_beacon(self):
        for path, html in self.html.items():
            with self.subTest(path=path):
                m = re.search(r'<body data-page="([a-z_]+)"', html)
                self.assertTrue(m, "no data-page, so beacons from this page are dropped")
                self.assertIn(m.group(1), app_module._TRACKED_PAGES)
                self.assertIn("dropzone_interact", html)

    def test_nav_cta_tagged_everywhere(self):
        for path, html in self.html.items():
            for tag in re.findall(r'<a[^>]*class="nav-cta"[^>]*>', html):
                if "/tc-check" in tag and "/tc-check/bulk" not in tag:
                    with self.subTest(path=path, tag=tag):
                        self.assertIn('data-evt="nav_check_cta"', tag)

    def test_every_tc_check_link_tagged(self):
        for path, html in self.html.items():
            for tag in re.findall(r'<a[^>]*href="/tc-check(?!/)[^"]*"[^>]*>', html):
                with self.subTest(path=path, tag=tag):
                    self.assertIn("data-evt=", tag)

    def test_every_data_evt_is_accepted(self):
        # A tag missing from _ENGAGEMENT_TYPES is silently dropped by /v1/evt.
        for path, html in self.html.items():
            for name in set(re.findall(r'data-evt="([a-z0-9_]+)"', html)):
                with self.subTest(path=path, evt=name):
                    self.assertIn(name, app_module._ENGAGEMENT_TYPES)

    def test_src_rewrites_still_match(self):
        client = app_module.app.test_client()
        home = client.get("/?src=li_test", headers=UA).get_data(as_text=True)
        self.assertIn('href="/tc-check?src=li_test" class="nav-cta" data-evt="nav_check_cta"', home)
        co = self.html["/tc-hub/colorado"]
        self.assertIn('href="/tc-check?state=co" class="nav-cta" data-evt="nav_check_cta"', co)

    def test_engagement_js_parses(self):
        path = os.path.join(_TMP, "evt.js")
        with open(path, "w") as f:
            f.write(app_module._ENGAGEMENT_JS.replace("<script>", "").replace("</script>", ""))
        try:
            subprocess.run(["node", "--check", path], check=True, capture_output=True)
        except FileNotFoundError:
            self.skipTest("node not installed")


class FunnelCountsAnyCheckCta(unittest.TestCase):
    def setUp(self):
        _clear_events()
        self.client = app_module.app.test_client()

    def _rows(self, event_type, rows):
        conn = sqlite3.connect(analytics.DB_PATH)
        conn.executemany("INSERT INTO events (event_type, phone, metadata, created_at) VALUES (?, NULL, ?, ?)",
                         [(event_type, json.dumps(m), datetime.utcnow().isoformat()) for m in rows])
        conn.commit()
        conn.close()

    def test_nav_click_on_pricing_is_recorded(self):
        self.assertEqual(_beacon(self.client, "nav_check_cta", "pricing_page", _vid(1)).status_code, 204)
        conn = sqlite3.connect(analytics.DB_PATH)
        got = [json.loads(m)["type"] for (m,) in conn.execute(
            "SELECT metadata FROM events WHERE event_type = 'page_engagement'")]
        conn.close()
        self.assertEqual(got, ["nav_check_cta"])

    def test_unknown_event_and_page_still_dropped(self):
        _beacon(self.client, "made_up_cta", "homepage", _vid(1))
        _beacon(self.client, "nav_check_cta", "made_up_page", _vid(1))
        self.assertEqual(analytics.get_daily_funnel(days=1)[0]["check_cta"], 0)

    def test_clicked_column_counts_unique_browsers_across_ctas(self):
        _beacon(self.client, "hero_cta", "homepage", _vid(1))        # old name still counts
        _beacon(self.client, "nav_check_cta", "homepage", _vid(1))   # same browser: still 1
        _beacon(self.client, "nav_check_cta", "trust_page", _vid(2))
        _beacon(self.client, "hub_check_cta", "tc_hub_page", _vid(3))
        _beacon(self.client, "tc_own_file_cta", "tc_check_page", _vid(4))
        _beacon(self.client, "stay_10s", "homepage", _vid(5))        # not a click
        today = analytics.get_daily_funnel(days=1)[0]
        self.assertEqual(today["check_cta"], 4)
        self.assertEqual(today["hero_cta"], 1)

    def test_sample_and_dropbox_interaction(self):
        _beacon(self.client, "hero_sample", "homepage", _vid(1))
        _beacon(self.client, "tc_sample_btn", "tc_check_page", _vid(2))
        _beacon(self.client, "dropzone_seen", "tc_check_page", _vid(2))
        _beacon(self.client, "dropzone_seen", "tc_check_page", _vid(3))
        _beacon(self.client, "dropzone_interact", "tc_check_page", _vid(3))
        today = analytics.get_daily_funnel(days=1)[0]
        self.assertEqual((today["sample_cta"], today["dropzone_seen"], today["dropzone_interact"]), (2, 2, 1))

    def test_device_table_uses_any_check_cta(self):
        _beacon(self.client, "nav_check_cta", "homepage", _vid(1))
        desktop = [r for r in analytics.get_engagement_by_device(days=1) if r["device"] == "desktop"][0]
        self.assertEqual(desktop["check_cta"], 1)

    def test_engagement_by_source(self):
        a, b, c = _vid(1), _vid(2), _vid(3)
        self._rows("page_view", [
            {"page": "homepage", "visitor": a, "source": "li_co_flyer", "device": "desktop", "ua": "Mac"},
            {"page": "tc_check_page", "visitor": a, "source": "fb_trei", "device": "desktop", "ua": "Mac"},  # later page: first src wins
            {"page": "tc_check_page", "visitor": b, "source": "ti_keydfw", "device": "desktop", "ua": "Linux Chrome/124"},
            {"page": "tc_check_page", "visitor": c, "source": "ig_bio", "device": "mobile", "ua": "iPhone"},
        ])
        self._rows("page_engagement", [
            {"type": "js_ok", "page": "homepage", "visitor": a}, {"type": "stay_10s", "page": "homepage", "visitor": a},
            {"type": "nav_check_cta", "page": "homepage", "visitor": a},
            {"type": "js_ok", "page": "tc_check_page", "visitor": c},
        ])
        self._rows("tc_check_demo_used", [{"source": "li_co_flyer", "visitor": a}])
        self._rows("tc_check_attempted", [{"source": "li_co_flyer", "visitor": a}])
        out = analytics.get_engagement_by_source(days=1)
        src = {r["key"]: r for r in out["sources"]}
        self.assertEqual({k: src["li_co_flyer"][k] for k in ("loads", "real", "stayed", "checked", "sampled", "uploaded")},
                         {"loads": 1, "real": 1, "stayed": 1, "checked": 1, "sampled": 1, "uploaded": 1})
        self.assertEqual((src["ti_keydfw"]["loads"], src["ti_keydfw"]["real"]), (1, 0))  # scanner: loaded, never ran JS
        self.assertEqual(src["fb_trei"]["real"], 0)
        ch = {r["key"]: r for r in out["channels"]}
        self.assertEqual(ch["LinkedIn"]["real"], 1)
        self.assertEqual(ch["Cold email"]["loads"], 1)
        self.assertEqual(ch["Instagram"]["real"], 1)
        self.assertEqual(ch["Facebook"]["loads"], 1)

    def test_channel_rollup(self):
        f = analytics._channel_for_source
        self.assertEqual(f("ad_yourcatch_linkedin"), "LinkedIn")
        self.assertEqual(f("ad_november31_fbtcg"), "Facebook")
        self.assertEqual(f("d1013_spyglass"), "Cold email")
        self.assertEqual(f("co1012_kittle"), "Cold email")
        self.assertEqual(f("zillow_broker_reach_oct"), "Cold email")
        self.assertEqual(f("seo_trec-20-19-checklist"), "Search (guides)")
        self.assertEqual(f("direct"), "direct (no tag)")
        self.assertEqual(f("something_new"), "other")

    def test_analytics_page_shows_new_columns_and_ua_filter(self):
        old = app_module.ANALYTICS_PASSWORD
        app_module.ANALYTICS_PASSWORD = "pw"
        try:
            self._rows("page_view", [{"page": "tc_check_page", "visitor": _vid(9), "source": "ti_keydfw",
                                      "device": "desktop", "ua": "Mozilla/5.0 (X11; Linux x86_64) Chrome/124.0"}])
            _beacon(self.client, "nav_check_cta", "homepage", _vid(8))
            html = self.client.get("/analytics?token=pw&src_ua=ti_", headers=UA).get_data(as_text=True)
        finally:
            app_module.ANALYTICS_PASSWORD = old
        self.assertIn("Used drop box", html)
        self.assertIn("Real Humans by Channel", html)
        self.assertIn("Visitors with src starting &ldquo;ti_&rdquo;", html)
        self.assertIn("Linux x86_64) Chrome/124.0", html)
        self.assertIn("clicked check (nav check cta)", html)


if __name__ == "__main__":
    unittest.main()
