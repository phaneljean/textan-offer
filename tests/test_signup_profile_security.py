"""Signed /profile and /dashboard links only reach the phone they're for
(security audit 2026-10-23). Two holes this pins shut:

1. /signup (and /success) put a signed profile link in the page for
   whatever phone was typed. The same signature opens /dashboard, so
   anyone's public phone number handed over their offers and PDF links.
2. /profile POST checked the signature against the query-string phone but
   saved to the form's phone, so any valid link (even the empty-phone one
   /success gave every visitor) could overwrite another agent's profile.

Twilio is mocked throughout.

    python3 -m unittest tests.test_signup_profile_security
"""
import hashlib
import hmac
import os
import re
import tempfile
import time
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY", "STRIPE_SECRET_KEY"):
    os.environ[_k] = ""

import app as app_module  # noqa: E402
from agent_profiles import get_agent_profile, save_agent_profile  # noqa: E402
from subscriptions import create_user, get_user  # noqa: E402

VICTIM = "+15125550199"
ATTACKER = "+15125550177"
_SIGNED = re.compile(r"/(?:profile|dashboard)\?[^\"'<\s]*sig=")


def _path(url):
    return url.split("localhost", 1)[-1]


class SignupNeverShowsSignedLink(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        if not get_user(VICTIM):
            create_user(VICTIM)

    def _signup(self, phone, checked=False):
        data = {"phone": phone, "name": "Someone", "email": "s@example.com"}
        if checked:
            data["sms_consent"] = "on"
        with mock.patch("app.twilio_send_sms", return_value=True) as tw:
            r = self.client.post("/signup", data=data)
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True), tw

    def test_typing_someone_elses_phone_shows_no_link(self):
        page, tw = self._signup(VICTIM)
        self.assertIsNone(_SIGNED.search(page))
        tw.assert_not_called()  # no consent box, no text
        self.assertIn("text PROFILE to (833) 897-0333", page)

    def test_link_goes_by_text_to_the_phone_only(self):
        page, tw = self._signup("+15125550188", checked=True)
        self.assertIsNone(_SIGNED.search(page))
        tw.assert_called_once()
        self.assertEqual(tw.call_args[0][0], "+15125550188")
        sms = tw.call_args[0][1]
        link = re.search(r"http://localhost(/profile\?\S+)", sms).group(1)
        self.assertEqual(self.client.get(link).status_code, 200)

    def test_existing_and_new_phone_get_the_same_page(self):
        existing, _ = self._signup(VICTIM)
        new, _ = self._signup("+15125550166")
        self.assertEqual(existing, new)

    def test_success_page_shows_no_link(self):
        page = self.client.get("/success").get_data(as_text=True)
        self.assertIsNone(_SIGNED.search(page))
        page = self.client.get("/success?session_id=cs_test_x").get_data(as_text=True)
        self.assertIsNone(_SIGNED.search(page))


class ProfileSavesOnlyToTheSignedPhone(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        save_agent_profile(VICTIM, {"name": "Real Agent", "email": "real@example.com", "license": "111"})

    def _victim_unchanged(self):
        p = get_agent_profile(VICTIM)
        self.assertEqual((p.get("name"), p.get("email"), p.get("license")),
                         ("Real Agent", "real@example.com", "111"))

    def test_own_link_cannot_save_over_another_phone(self):
        link = _path(app_module.sign_dashboard_url(ATTACKER).replace("/dashboard?", "/profile?"))
        r = self.client.post(link, data={"phone": VICTIM, "name": "attacker", "email": "evil@example.com", "license": "999"})
        self.assertEqual(r.status_code, 403)
        self._victim_unchanged()

    def test_empty_phone_link_is_rejected(self):
        expires = int(time.time()) + 3600
        sig = hmac.new(app_module.PDF_LINK_SECRET.encode(), f"dash::{expires}".encode(), hashlib.sha256).hexdigest()[:20]
        link = f"/profile?phone=&expires={expires}&sig={sig}"
        self.assertEqual(self.client.get(link).status_code, 403)
        r = self.client.post(link, data={"phone": VICTIM, "name": "attacker"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.get(link.replace("/profile?", "/dashboard?")).status_code, 403)
        self._victim_unchanged()

    def test_owner_can_still_save(self):
        link = _path(app_module.sign_dashboard_url(VICTIM).replace("/dashboard?", "/profile?"))
        r = self.client.post(link, data={"phone": VICTIM, "name": "real agent", "license": "222"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(get_agent_profile(VICTIM).get("license"), "222")
        save_agent_profile(VICTIM, {"name": "Real Agent", "email": "real@example.com", "license": "111"})

    def test_form_posts_only_hidden_fields_without_query(self):
        # The page's own form posts phone/expires/sig in the body (no query
        # string) -- that path must keep working for a real owner.
        link = app_module.sign_dashboard_url(VICTIM)
        q = dict(kv.split("=", 1) for kv in link.split("?", 1)[1].split("&"))
        r = self.client.post("/profile", data={"phone": VICTIM, "expires": q["expires"], "sig": q["sig"], "name": "Real Agent",
                                               "email": "real@example.com", "license": "111"})
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
