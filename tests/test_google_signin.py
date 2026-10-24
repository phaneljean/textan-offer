"""Sign in with Google for the brokerage archive (2026-10-24): off until
GOOGLE_CLIENT_ID/SECRET are set; Google only proves the email, the
brokerage's login-email list still decides access; a forged, replayed,
mismatched or unverified sign-in never gets a session.

    python3 -m unittest tests.test_google_signin
"""
import base64
import json
import os
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlparse
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
os.environ["ANALYTICS_PASSWORD"] = "test-internal"
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import app as app_module  # noqa: E402
import broker_auth  # noqa: E402
import brokerages  # noqa: E402
import google_auth  # noqa: E402

CID = "test-client.apps.googleusercontent.com"
ON = {"GOOGLE_CLIENT_ID": CID, "GOOGLE_CLIENT_SECRET": "test-secret"}
OFF = {"GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": ""}
TC = "tc@google-test.example"


def _id_token(**claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{body}.sig"


class GoogleSignIn(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = brokerages.create_brokerage("Google Test Realty", tc_email=TC)

    def setUp(self):
        self.c = app_module.app.test_client()

    def _start(self):
        r = self.c.get("/broker/login/google")
        self.assertEqual(r.status_code, 302)
        q = parse_qs(urlparse(r.headers["Location"]).query)
        cookie = self.c.get_cookie(google_auth.STATE_COOKIE).value
        state, nonce = cookie.split("|")[:2]
        self.assertEqual(q["state"][0], state)
        self.assertEqual(q["nonce"][0], nonce)
        return state, nonce

    def _callback(self, state, claims, **extra):
        token = {"id_token": _id_token(**claims)}
        with mock.patch.object(google_auth, "_exchange_code", return_value=token):
            return self.c.get("/broker/login/google/callback", query_string={"state": state, "code": "abc", **extra})

    def _good(self, nonce, **over):
        claims = {"iss": "https://accounts.google.com", "aud": CID, "exp": int(time.time()) + 600,
                  "nonce": nonce, "email": TC.upper(), "email_verified": True}
        claims.update(over)
        return claims

    def _session(self):
        c = self.c.get_cookie(broker_auth.COOKIE)
        return c.value if c else ""

    # --- off by default ---
    def test_off_without_keys(self):
        with mock.patch.dict(os.environ, OFF):
            self.assertNotIn("Sign in with Google", self.c.get("/broker/login").get_data(as_text=True))
            self.assertEqual(self.c.get("/broker/login/google").status_code, 404)
            self.assertEqual(self.c.get("/broker/login/google/callback").status_code, 404)
            self.assertNotIn("Google (Sign-in)", self.c.get("/privacy").get_data(as_text=True))
            self.assertNotIn("Sign in with Google</h3>", self.c.get("/trust").get_data(as_text=True))

    # --- on ---
    def test_button_and_redirect(self):
        with mock.patch.dict(os.environ, ON):
            self.assertIn("Sign in with Google", self.c.get("/broker/login").get_data(as_text=True))
            r = self.c.get("/broker/login/google")
        u = urlparse(r.headers["Location"])
        self.assertEqual(u.netloc, "accounts.google.com")
        q = parse_qs(u.query)
        self.assertEqual(q["client_id"][0], CID)
        self.assertEqual(q["scope"][0], "openid email")
        self.assertEqual(q["redirect_uri"][0], "https://txtanoffer.com/broker/login/google/callback")

    def test_good_sign_in_opens_the_archive(self):
        with mock.patch.dict(os.environ, ON):
            state, nonce = self._start()
            r = self._callback(state, self._good(nonce))
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r.headers["Location"].endswith("/broker/archive"))
            self.assertEqual(broker_auth.read_session(self._session()), (self.b["id"], TC))
            self.assertEqual(self.c.get("/broker/archive").status_code, 200)

    def _refused(self, r, code=400):
        self.assertEqual(r.status_code, code)
        self.assertFalse(self._session())

    def test_wrong_state_refused(self):
        with mock.patch.dict(os.environ, ON):
            _, nonce = self._start()
            self._refused(self._callback("forged-state", self._good(nonce)))

    def test_no_state_cookie_refused(self):
        with mock.patch.dict(os.environ, ON):
            self._refused(self._callback("anything", self._good("n")))

    def test_token_for_another_app_refused(self):
        with mock.patch.dict(os.environ, ON):
            state, nonce = self._start()
            self._refused(self._callback(state, self._good(nonce, aud="someone-else")))

    def test_wrong_nonce_refused(self):
        with mock.patch.dict(os.environ, ON):
            state, _ = self._start()
            self._refused(self._callback(state, self._good("replayed-nonce")))

    def test_wrong_issuer_refused(self):
        with mock.patch.dict(os.environ, ON):
            state, nonce = self._start()
            self._refused(self._callback(state, self._good(nonce, iss="https://evil.example")))

    def test_expired_token_refused(self):
        with mock.patch.dict(os.environ, ON):
            state, nonce = self._start()
            self._refused(self._callback(state, self._good(nonce, exp=int(time.time()) - 5)))

    def test_unverified_email_refused(self):
        with mock.patch.dict(os.environ, ON):
            state, nonce = self._start()
            self._refused(self._callback(state, self._good(nonce, email_verified=False)))

    def test_email_not_on_any_brokerage_refused(self):
        with mock.patch.dict(os.environ, ON):
            state, nonce = self._start()
            self._refused(self._callback(state, self._good(nonce, email="stranger@gmail.com")), 403)

    def test_cancelled_at_google(self):
        with mock.patch.dict(os.environ, ON):
            state, _ = self._start()
            r = self.c.get("/broker/login/google/callback", query_string={"state": state, "error": "access_denied"})
        self._refused(r)

    def test_pages_mention_google_only_when_on(self):
        with mock.patch.dict(os.environ, ON):
            self.assertIn("Sign in with Google</h3>", self.c.get("/trust").get_data(as_text=True))
            self.assertIn("Okta or SAML single sign-on", self.c.get("/trust").get_data(as_text=True))
            self.assertIn("Google (Sign-in)", self.c.get("/privacy").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
