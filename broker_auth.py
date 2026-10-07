"""
broker_auth.py -- Email sign-in for the brokerage archive (2026-10-07).

The join-code dashboard link was fine for roster stats, but stored executed
contracts need a real sign-in. Flow: broker enters their email at
/broker/login -> if it's one of the brokerage's login emails (TC email or
login_emails), a one-time link valid for LINK_TTL is emailed -> clicking it
sets a signed, HttpOnly session cookie valid for SESSION_TTL. No passwords
are stored anywhere. Signatures are HMAC-SHA256 with a purpose prefix so a
login-link signature can never be replayed as a session (or vice versa).
"""
import hashlib
import hmac
import os
import time

SECRET = os.environ.get("BROKER_AUTH_SECRET") or os.environ.get("PDF_LINK_SECRET", "change-me-in-production")
LINK_TTL = 20 * 60
SESSION_TTL = 14 * 24 * 3600
COOKIE = "ta_broker"


def _sig(purpose: str, *parts) -> str:
    msg = purpose + ":" + ":".join(str(p) for p in parts)
    return hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()


def login_link_params(brokerage_id: int, email: str) -> dict:
    exp = int(time.time()) + LINK_TTL
    return {"b": brokerage_id, "e": email.lower(), "exp": exp, "sig": _sig("broker_login", brokerage_id, email.lower(), exp)}


def verify_login_link(b: str, e: str, exp: str, sig: str):
    """Returns the brokerage id if the link is valid and unexpired, else None."""
    try:
        bid, exp_i = int(b), int(exp)
    except (TypeError, ValueError):
        return None
    if time.time() > exp_i:
        return None
    if not hmac.compare_digest(sig or "", _sig("broker_login", bid, (e or "").lower(), exp_i)):
        return None
    return bid


def session_value(brokerage_id: int, email: str) -> str:
    exp = int(time.time()) + SESSION_TTL
    return f"{brokerage_id}|{email.lower()}|{exp}|{_sig('broker_session', brokerage_id, email.lower(), exp)}"


def read_session(cookie_value: str):
    """(brokerage_id, email) for a valid session cookie, else None."""
    try:
        bid, email, exp, sig = (cookie_value or "").split("|")
        bid_i, exp_i = int(bid), int(exp)
    except ValueError:
        return None
    if time.time() > exp_i:
        return None
    if not hmac.compare_digest(sig, _sig("broker_session", bid_i, email, exp_i)):
        return None
    return bid_i, email


def csrf_token(cookie_value: str) -> str:
    return _sig("broker_csrf", cookie_value or "")[:32]
