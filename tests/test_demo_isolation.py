"""/demo per-visitor isolation (2026-10-14). Until then every /demo visitor
shared one pending-draft key, so a second visitor's YES generated and showed
the first visitor's address and price. Each test client below is a separate
browser with its own cookie jar. Also covers /demo's ?src= page_view and
the honest-copy rewrite.

    python3 -m unittest tests.test_demo_isolation
"""
import json
import os
import re
import sqlite3
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import analytics
import app as app_module
from drafts import get_draft

UA = {"User-Agent": "Mozilla/5.0 (Macintosh) test"}
A_OFFER = "725k 3% 21day 123 Main St, Austin, TX 78701"
B_OFFER = "410k 10% 30day 456 Oak Ave, Houston, TX 77002"


def _post(client, text):
    return client.post("/demo", data={"offer_text": text}, headers=UA)


def _demo_token(client):
    c = client.get_cookie(app_module._DEMO_COOKIE)
    return c.value if c else None


class DemoVisitorsAreIsolated(unittest.TestCase):
    def setUp(self):
        self.a = app_module.app.test_client()
        self.b = app_module.app.test_client()

    def test_each_browser_gets_its_own_token(self):
        self.a.get("/demo", headers=UA)
        self.b.get("/demo", headers=UA)
        ta, tb = _demo_token(self.a), _demo_token(self.b)
        self.assertRegex(ta, r"^[0-9a-f]{32}$")
        self.assertRegex(tb, r"^[0-9a-f]{32}$")
        self.assertNotEqual(ta, tb)
        # Returning visitor keeps the same token (no Set-Cookie churn).
        r = self.a.get("/demo", headers=UA)
        self.assertNotIn(app_module._DEMO_COOKIE, r.headers.get("Set-Cookie", ""))
        self.assertEqual(_demo_token(self.a), ta)

    def test_yes_from_other_browser_does_not_confirm_or_show_draft(self):
        r = _post(self.a, A_OFFER)
        self.assertIn("Confirm Offer", r.get_data(as_text=True))
        self.assertIsNotNone(get_draft(f"demo-web:{_demo_token(self.a)}"))

        self.b.get("/demo", headers=UA)
        html = _post(self.b, "YES").get_data(as_text=True)
        self.assertIn("No pending offer to confirm", html)
        result = html.split("</form>")[1].split('<div class="cmd-menu"')[0]
        self.assertNotIn("123 Main St", result)
        self.assertNotIn("725,000", html)
        self.assertNotIn("/offers/", html)
        # B's NO can't cancel A's draft either.
        self.assertIn("Nothing pending to cancel", _post(self.b, "NO").get_data(as_text=True))
        self.assertIsNotNone(get_draft(f"demo-web:{_demo_token(self.a)}"))

    def test_concurrent_drafts_each_confirm_their_own(self):
        _post(self.a, A_OFFER)
        _post(self.b, B_OFFER)
        ha = _post(self.a, "YES").get_data(as_text=True)
        hb = _post(self.b, "YES").get_data(as_text=True)
        self.assertIn("725,000", ha)
        self.assertNotIn("410,000", ha)
        self.assertIn("410,000", hb)
        self.assertNotIn("725,000", hb)
        self.assertIn("/offers/", ha)
        self.assertIn("/offers/", hb)
        # Offer history is keyed per browser too, so AMEND can't reach the other's.
        amend = _post(self.b, "AMEND 123 Main St price 730k").get_data(as_text=True)
        self.assertIn("No demo offer found", amend)
        self.assertIn("Amendment (TREC 39-11)", _post(self.a, "AMEND 123 Main St price 730k").get_data(as_text=True))

    def test_yes_with_no_cookie_and_no_draft_does_nothing(self):
        _post(self.a, A_OFFER)
        fresh = app_module.app.test_client()
        html = _post(fresh, "YES").get_data(as_text=True)
        self.assertIn("No pending offer to confirm", html)
        self.assertNotIn("/offers/", html)

    def test_forged_cookie_is_replaced(self):
        self.b.set_cookie(app_module._DEMO_COOKIE, "not-a-token")
        _post(self.b, "YES")
        self.assertRegex(_demo_token(self.b), r"^[0-9a-f]{32}$")


class DemoSrcIsTracked(unittest.TestCase):
    def test_src_lands_in_page_view(self):
        conn = sqlite3.connect(analytics.DB_PATH)
        conn.execute("DELETE FROM events")
        conn.commit()
        html = app_module.app.test_client().get("/demo?src=ad_test_demo", headers=UA).get_data(as_text=True)
        self.assertIn('<body data-page="demo_page">', html)
        self.assertIn("demo_page", app_module._TRACKED_PAGES)
        views = [json.loads(m) for (m,) in conn.execute(
            "SELECT metadata FROM events WHERE event_type = 'page_view'")]
        conn.close()
        self.assertEqual([(v["page"], v["source"]) for v in views], [("demo_page", "ad_test_demo")])
        srcs = [r["key"] for r in analytics.get_engagement_by_source(days=1)["sources"]]
        self.assertIn("ad_test_demo", srcs)


class DemoCopyIsHonest(unittest.TestCase):
    def test_no_speed_or_finished_contract_claims(self):
        c = app_module.app.test_client()
        page = c.get("/demo", headers=UA).get_data(as_text=True)
        _post(c, A_OFFER)
        result = _post(c, "YES").get_data(as_text=True)
        for html in (page, result):
            for claim in ("10 seconds", "&lt;10s", "instantly", "3 seconds", "completed TREC", "Contract ready"):
                self.assertNotIn(claim, html)
        self.assertIn("get a draft to review", page)
        self.assertIn("CBS1", page)
        self.assertIn("before signing", page)


if __name__ == "__main__":
    unittest.main()
