"""PWA installability + install tracking (2026-11-11): the manifest and
service worker routes, the four icons on disk, the head wiring on the
homepage / dashboard / notepad pages, the "Install the app" block, and
the signature-verified path that lets install events from signed pages
carry the agent's phone into /v1/evt (plus the /analytics "App installs"
list that reads them back).

    python3 -m unittest tests.test_pwa
"""
import json
import os
import re
import sqlite3
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
os.environ["ANALYTICS_PASSWORD"] = "test-internal"
# Throwaway Fernet key: some page renders touch filecrypt, which 500s on
# a malformed key. Test-only value, generated for this module.
os.environ["FILE_ENCRYPTION_KEY"] = "atRceqVlpntRmPCCJTL7zPNO0FWxsQCyOKQb599cv-0="
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import analytics  # noqa: E402
import app as app_module  # noqa: E402
import subscriptions  # noqa: E402
from PIL import Image  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) test"}
PWA_TYPES = ("pwa_install_accepted", "pwa_installed", "pwa_standalone_launch")


def _signed(phone):
    url = app_module.sign_dashboard_url(phone, "http://localhost")
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def _pwa_rows(phone=None):
    conn = sqlite3.connect(analytics.DB_PATH)
    try:
        rows = conn.execute(
            "SELECT phone, metadata FROM events WHERE event_type = 'page_engagement'"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for ph, meta in rows:
        m = json.loads(meta) if meta else {}
        if m.get("type") in PWA_TYPES and (phone is None or ph == phone):
            out.append((ph, m))
    return out


class ManifestAndWorker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()

    def test_manifest_shape(self):
        resp = self.client.get("/manifest.json")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("application/manifest+json", resp.content_type)
        m = json.loads(resp.get_data(as_text=True))
        self.assertEqual(m["name"], "TxtAnOffer")
        self.assertEqual(m["short_name"], "TxtAnOffer")
        self.assertEqual(m["start_url"], "/notepad")
        self.assertEqual(m["scope"], "/")
        self.assertEqual(m["display"], "standalone")
        self.assertEqual(m["theme_color"], "#0b5d52")
        by_size = {(i["sizes"], i["purpose"]) for i in m["icons"]}
        self.assertIn(("192x192", "any"), by_size)
        self.assertIn(("512x512", "any"), by_size)
        self.assertIn(("512x512", "maskable"), by_size)

    def test_manifest_icons_exist_with_matching_dimensions(self):
        m = json.loads(self.client.get("/manifest.json").get_data(as_text=True))
        for icon in m["icons"]:
            path = os.path.join(ROOT, icon["src"].lstrip("/"))
            self.assertTrue(os.path.isfile(path), icon["src"])
            w, h = (int(x) for x in icon["sizes"].split("x"))
            with Image.open(path) as im:
                self.assertEqual(im.size, (w, h), icon["src"])
        apple = os.path.join(ROOT, "static", "pwa", "apple-touch-icon.png")
        with Image.open(apple) as im:
            self.assertEqual(im.size, (180, 180))

    def test_service_worker_is_install_only(self):
        resp = self.client.get("/sw.js")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("javascript", resp.content_type)
        self.assertEqual(resp.headers.get("Service-Worker-Allowed"), "/")
        src = resp.get_data(as_text=True)
        # The guard the whole design rests on: query-string (signed) URLs
        # are never cached, and only GET same-origin precache paths are
        # ever answered from cache.
        self.assertIn("PWA_GUARD", src)
        self.assertIn("url.search", src)
        self.assertIn("txo-pwa-v1", src)


class HeadWiringAndInstallBlock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()
        cls.phone = "+15145550101"
        subscriptions.create_user(cls.phone)
        cls.qs = _signed(cls.phone)

    def test_homepage_carries_manifest_link(self):
        html = self.client.get("/", headers=UA).get_data(as_text=True)
        self.assertIn('rel="manifest"', html)
        self.assertIn('name="theme-color"', html)
        self.assertIn("apple-touch-icon", html)
        self.assertIn("serviceWorker.register('/sw.js')", html)

    def test_homepage_carries_install_block(self):
        # The homepage desk block reuses the same ids, so the shared
        # PWA_JS snippet drives it with no homepage-specific wiring.
        html = self.client.get("/", headers=UA).get_data(as_text=True)
        self.assertIn('id="txoInstallBtn"', html)
        self.assertIn("Install the app", html)
        self.assertIn("Add to Home Screen", html)

    def test_dashboard_carries_head_tags_and_install_block(self):
        resp = self.client.get("/dashboard", query_string=self.qs, headers=UA)
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('rel="manifest"', html)
        self.assertIn('id="txoInstallBtn"', html)
        self.assertIn("Install the app", html)
        self.assertIn("Add to Home Screen", html)

    def test_notepad_list_carries_install_block(self):
        resp = self.client.get("/notepad", query_string=self.qs, headers=UA)
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('rel="manifest"', html)
        self.assertIn('id="txoInstallBtn"', html)

    def test_install_wrapper_is_always_visible(self):
        # The button must render on page load, everywhere the block
        # appears: neither wrapper may start hidden behind display:none
        # (it used to appear only if beforeinstallprompt fired, so for
        # already-installed / dismissed / unsupported visitors the
        # download option simply did not exist on screen).
        pages = [
            self.client.get("/", headers=UA).get_data(as_text=True),
            self.client.get("/dashboard", query_string=self.qs,
                            headers=UA).get_data(as_text=True),
            self.client.get("/notepad", query_string=self.qs,
                            headers=UA).get_data(as_text=True),
        ]
        for html in pages:
            m = re.search(r'<div[^>]*id="txoInstall"[^>]*>', html)
            self.assertIsNotNone(m)
            self.assertNotIn("display:none", m.group(0))
            self.assertNotIn("display: none", m.group(0))


class InstallTracking(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()

    def test_event_types_registered(self):
        for t in PWA_TYPES:
            self.assertIn(t, app_module._ENGAGEMENT_TYPES)
            self.assertIn(t, app_module._PWA_EVENT_TYPES)

    def test_install_js_fires_taevt(self):
        for t in PWA_TYPES:
            self.assertIn(f"taEvt('{t}')", app_module.PWA_JS)
        self.assertIn("beforeinstallprompt", app_module.PWA_JS)
        self.assertIn("appinstalled", app_module.PWA_JS)
        self.assertIn("display-mode: standalone", app_module.PWA_JS)
        self.assertIn("txo_pwa_dismissed", app_module.PWA_JS)

    def test_install_click_fallback_instructions(self):
        # With no deferred beforeinstallprompt, the click reveals
        # manual install instructions (per-platform text) instead of
        # hiding the block; pwa_install_accepted stays exclusive to
        # the native prompt's userChoice (asserted above).
        js = app_module.PWA_JS
        self.assertIn("showHint()", js)
        self.assertIn("To install: Share \u2192 Add to Home Screen", js)
        self.assertIn('open your browser menu and choose "Install app" '
                      'or "Add to Home Screen".', js)

    def test_signed_beacon_records_phone(self):
        phone = "+15145550102"
        subscriptions.create_user(phone)
        qs = _signed(phone)
        resp = self.client.post("/v1/evt", data={
            "type": "pwa_installed", "page": "notepad",
            "phone": qs["phone"], "expires": qs["expires"], "sig": qs["sig"],
        }, headers=UA)
        self.assertEqual(resp.status_code, 204)
        rows = _pwa_rows(phone)
        self.assertTrue(any(m["type"] == "pwa_installed" and m["page"] == "notepad"
                            for _, m in rows), rows)

    def test_forged_signature_records_nothing(self):
        phone = "+15145550103"
        subscriptions.create_user(phone)
        qs = _signed(phone)
        resp = self.client.post("/v1/evt", data={
            "type": "pwa_installed", "page": "dashboard",
            "phone": qs["phone"], "expires": qs["expires"], "sig": "0" * 64,
        }, headers=UA)
        self.assertEqual(resp.status_code, 204)  # always 204; just unrecorded
        self.assertEqual(_pwa_rows(phone), [])

    def test_analytics_page_lists_installs(self):
        phone = "+15145550104"
        analytics.track_event("page_engagement", phone, {
            "type": "pwa_installed", "page": "dashboard",
            "visitor": "", "device": "mobile",
        })
        old = app_module.ANALYTICS_PASSWORD
        app_module.ANALYTICS_PASSWORD = "test-internal"
        try:
            resp = self.client.get("/analytics", query_string={"token": "test-internal"},
                                   headers=UA)
        finally:
            app_module.ANALYTICS_PASSWORD = old
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("App installs", html)
        self.assertIn(phone, html)
        self.assertIn("pwa_installed", html)


if __name__ == "__main__":
    unittest.main()
