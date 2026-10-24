"""
google_auth.py -- "Sign in with Google" for the brokerage archive (2026-10-24).

An alternative to the emailed one-time link (broker_auth.py), not a new kind
of account: Google only proves the person controls an email address, and the
same rule as before decides access -- the email must be one of the
brokerage's login emails (archive.login_emails_for). Off until both
GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are set.

Flow (OAuth 2.0 authorization code, OpenID Connect):
  /broker/login/google           -> state + nonce in a signed, HttpOnly,
                                    10-minute cookie; redirect to Google
  /broker/login/google/callback  -> state must match the cookie; the code is
                                    exchanged server-to-server over HTTPS, so
                                    the ID token comes straight from Google;
                                    iss, aud, exp, nonce and email_verified
                                    are still checked before it's trusted.

Setup (Phanel, Google Cloud console): OAuth client of type "Web
application", authorized redirect URI = GOOGLE_REDIRECT_URI (default
https://txtanoffer.com/broker/login/google/callback), scopes openid + email.
"""
import base64
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

import broker_auth

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
STATE_COOKIE = "ta_g_state"
STATE_TTL = 10 * 60


class GoogleAuthError(Exception):
    pass


def _client():
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip(), os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()


def enabled() -> bool:
    cid, secret = _client()
    return bool(cid and secret)


def redirect_uri() -> str:
    return os.environ.get("GOOGLE_REDIRECT_URI", "https://txtanoffer.com/broker/login/google/callback")


def start():
    """(Google URL to redirect to, value for STATE_COOKIE)."""
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    exp = int(time.time()) + STATE_TTL
    cookie = f"{state}|{nonce}|{exp}|{broker_auth._sig('google_state', state, nonce, exp)}"
    params = {
        "client_id": _client()[0], "redirect_uri": redirect_uri(), "response_type": "code",
        "scope": "openid email", "state": state, "nonce": nonce, "prompt": "select_account",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params), cookie


def _read_state_cookie(cookie: str):
    try:
        state, nonce, exp, sig = (cookie or "").split("|")
        exp_i = int(exp)
    except ValueError:
        raise GoogleAuthError("Sign-in session missing. Start again.")
    if time.time() > exp_i or not hmac.compare_digest(sig, broker_auth._sig("google_state", state, nonce, exp_i)):
        raise GoogleAuthError("Sign-in session expired. Start again.")
    return state, nonce


def _exchange_code(code: str) -> dict:
    cid, secret = _client()
    body = urllib.parse.urlencode({
        "code": code, "client_id": cid, "client_secret": secret,
        "redirect_uri": redirect_uri(), "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, ValueError) as e:
        raise GoogleAuthError(f"Google didn't confirm the sign-in ({e.__class__.__name__}).")


def _claims(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        raise GoogleAuthError("Google sent an unreadable sign-in token.")


def finish(args, state_cookie: str) -> str:
    """Verified, lower-cased email for a Google callback, or GoogleAuthError."""
    if args.get("error"):
        raise GoogleAuthError("Google sign-in was cancelled.")
    state, nonce = _read_state_cookie(state_cookie)
    if not hmac.compare_digest(args.get("state", ""), state):
        raise GoogleAuthError("Sign-in session didn't match. Start again.")
    code = args.get("code", "")
    if not code:
        raise GoogleAuthError("Google didn't send a sign-in code.")
    claims = _claims(_exchange_code(code).get("id_token", ""))
    if claims.get("iss") not in ISSUERS:
        raise GoogleAuthError("Sign-in token wasn't issued by Google.")
    if claims.get("aud") != _client()[0]:
        raise GoogleAuthError("Sign-in token was meant for another app.")
    if int(claims.get("exp", 0)) < time.time():
        raise GoogleAuthError("Sign-in token expired. Start again.")
    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise GoogleAuthError("Sign-in session didn't match. Start again.")
    if claims.get("email_verified") not in (True, "true"):
        raise GoogleAuthError("That Google account's email isn't verified.")
    email = (claims.get("email") or "").strip().lower()
    if "@" not in email:
        raise GoogleAuthError("Google didn't share an email address.")
    return email
