"""
app.py — Twilio SMS webhook for TxtAnOffer, plus a /demo web form that
bypasses SMS entirely (for testing while A2P 10DLC registration is pending).

Flow (SMS):
  Agent texts "725k 3% 21day 123 Main St"
    -> parse_offer_sms() extracts structured data
    -> (stub) pull real bed/bath/sqft from MLS -- replace with real API call
    -> fill_offer_pdf() writes values into 20-19_2.pdf
    -> reply with a summary + link to review/sign

Flow (demo, no SMS/Twilio needed):
  Visit /demo -> type the same offer string into a web form -> same
  parse/fill logic runs -> result + PDF link shown directly on the page.
"""

from flask import Flask, request, send_from_directory, Response, redirect, jsonify, abort, make_response
from datetime import datetime, timedelta
import os
import hmac
import hashlib
import time
from urllib.parse import quote as _urlquote
import stripe
import difflib
import requests as http_requests
from twilio.rest import Client as TwilioClient

from parser import parse_offer_sms, parse_amendment_sms, parse_correction_sms
from pdf_filler import fill_offer_pdf, OUTPUT_DIR
from pdf_validator import validate_offer_pdf
from amendment import fill_amendment_pdf
from agent_profiles import get_agent_profile, save_agent_profile
from subscriptions import can_generate_offer, increment_offer_count, activate_subscription, deactivate_subscription, get_user, create_user, FREE_OFFER_LIMIT, is_admin_phone, has_professional_access
from analytics import track_event, get_conversion_metrics, get_revenue_metrics, get_recent_sms, get_recent_sms_failures, get_last_blocked_state, get_waitlist_signups, get_signups_by_source, get_landing_visits_by_source, get_tc_check_summary
from integrations import send_offer_email, fire_webhook, save_webhook, get_webhook, delete_webhook, send_to_docusign
from offers_db import record_offer, get_offers_for_phone, get_offer_by_filename, record_amendment, get_amendments_for_phone, record_thread_response, record_email_sent
from sms_utils import parse_incoming_sms
from cleanup import run_cleanup_if_due
from reminders import run_reminders_if_due
from drafts import save_draft, get_draft, clear_draft
from tc_audit import check_tc_file
from rate_limit import check_and_increment
from tc_gate import get_client as get_tc_client, record_use as record_tc_use, save_email as save_tc_email
from tc_nudge import send_immediate_nudge as send_tc_nudge, run_followup_if_due as run_tc_followup_if_due
from werkzeug.middleware.proxy_fix import ProxyFix
import tempfile
import uuid

app = Flask(__name__)
# Railway terminates TLS at its edge and forwards plain HTTP internally, so
# without this, request.host_url (used to build every SMS/PDF/checkout link)
# reports "http://" even though the site is only ever served over https.
# Trusts exactly one proxy hop (Railway's own edge) for X-Forwarded-Proto/Host/For.
# x_for=1 makes request.remote_addr the real client IP instead of Railway's edge
# IP -- added alongside the /v1/tc/check rate limiter, which is useless without it
# (every request would otherwise appear to come from the same proxy address).
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1)
# No upload endpoint existed before /v1/tc/check -- this caps request body
# size app-wide so an unauthenticated upload can't tie up a worker with a
# huge file. 15MB is generous for an unflattened AcroForm PDF (this app's
# own generated offers run well under 1MB) and small enough to reject abuse.
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024

# Stripe configuration
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
# Pinned above the stripe==10.12.0 library default (2024-06-20) -- the
# account has Managed Payments enabled, which that older API version
# doesn't support and rejects Checkout Session creation outright.
stripe.api_version = "2025-03-31.basil"
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")
STRIPE_PRICE_ID_PRO = os.environ.get("STRIPE_PRICE_ID_PRO", "")
STRIPE_PRICE_ID_BROKERAGE = os.environ.get("STRIPE_PRICE_ID_BROKERAGE", "")

# TREC's mandatory-use date for the 05-04-2026 revision printed on every page
# footer of 20-19_2.pdf -- update this alongside the template whenever TREC
# republishes the form or moves the mandatory-use date.
TREC_FORM_CURRENT_AS_OF = "July 1, 2026"

PDF_LINK_SECRET = os.environ.get("PDF_LINK_SECRET", "change-me-in-production")
PDF_LINK_TTL = int(os.environ.get("PDF_LINK_TTL", 86400))  # 24 hours

# API auth for integration endpoints
API_BEARER_TOKEN = os.environ.get("API_BEARER_TOKEN", "")

# Analytics dashboard password
ANALYTICS_PASSWORD = os.environ.get("ANALYTICS_PASSWORD", "")

# Twilio configuration
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER", "+18338970333")


def require_api_auth():
    """Check Bearer token on integration endpoints. Returns error response or None."""
    if not API_BEARER_TOKEN:
        return jsonify({"error": "API not configured (missing API_BEARER_TOKEN)"}), 503
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or not hmac.compare_digest(auth[7:], API_BEARER_TOKEN):
        return jsonify({"error": "Unauthorized"}), 401
    return None


def require_api_or_pdf_signature_auth(pdf_filename, expires, sig):
    """Either a valid Bearer token (real server-to-server API callers) or a
    valid signature for one specific PDF -- the review page's own "Send to
    DocuSign"/"Webhook" buttons, which can never safely hold the server's
    static API token (anything sent to a browser is visible to that
    browser's user). The review page already only loads behind a signed
    link, so re-checking that same signature here proves the caller
    legitimately has access to this agent's offer, which is what actually
    matters -- without ever exposing the real API secret client-side.
    Returns an error response, or None if authorized."""
    if pdf_filename and verify_pdf_signature(pdf_filename, expires, sig):
        return None
    return require_api_auth()


def _is_safe_webhook_url(url):
    """Block private/reserved IPs and non-HTTPS URLs to prevent SSRF."""
    from urllib.parse import urlparse
    import ipaddress
    import socket

    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    try:
        resolved = socket.getaddrinfo(hostname, None)
        for _, _, _, _, addr in resolved:
            ip = ipaddress.ip_address(addr[0])
            if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
                return False
    except (socket.gaierror, ValueError):
        return False
    return True


def sign_pdf_view_params(filename):
    expires = int(time.time()) + PDF_LINK_TTL
    sig = hmac.new(PDF_LINK_SECRET.encode(), f"{filename}:{expires}".encode(), hashlib.sha256).hexdigest()[:16]
    return expires, sig


def sign_pdf_url(filename, base_url=""):
    expires, sig = sign_pdf_view_params(filename)
    return f"{base_url}/review/{filename}?expires={expires}&sig={sig}"


def verify_pdf_signature(filename, expires_str, sig):
    try:
        expires = int(expires_str)
    except (ValueError, TypeError):
        return False
    if time.time() > expires:
        return False
    expected = hmac.new(PDF_LINK_SECRET.encode(), f"{filename}:{expires}".encode(), hashlib.sha256).hexdigest()[:16]
    return hmac.compare_digest(sig or "", expected)


# --- Offer Thread (listing-agent Accept/Decline) signing ---------------
#
# Purpose-scoped, same precedent as sign_dashboard_url below: the payload
# is prefixed ("thread:"/"respond:") so this signature space can't be
# replayed against /review, /offers, or /dashboard's own schemes, which
# each sign a differently-shaped (or unprefixed) payload with the same
# PDF_LINK_SECRET. The respond action is signed separately from the view
# link (with `action` itself bound into the signature) so a forwarded/
# rewritten POST can't flip an accept into a decline without invalidating
# the signature -- the view signature alone wouldn't cover which button
# was actually pressed.

THREAD_LINK_TTL = int(os.environ.get("THREAD_LINK_TTL", 604800))  # 7 days -- a listing
# agent responding plausibly takes days, unlike the 24h PDF_LINK_TTL meant for a
# buyer's agent reviewing their own just-generated PDF.


def sign_thread_url(filename, base_url=""):
    expires = int(time.time()) + THREAD_LINK_TTL
    sig = hmac.new(PDF_LINK_SECRET.encode(), f"thread:{filename}:{expires}".encode(), hashlib.sha256).hexdigest()[:16]
    return f"{base_url}/thread/{filename}?expires={expires}&sig={sig}"


def verify_thread_signature(filename, expires_str, sig):
    try:
        expires = int(expires_str)
    except (ValueError, TypeError):
        return False
    if time.time() > expires:
        return False
    expected = hmac.new(PDF_LINK_SECRET.encode(), f"thread:{filename}:{expires}".encode(), hashlib.sha256).hexdigest()[:16]
    return hmac.compare_digest(sig or "", expected)


def sign_thread_action(filename, action, expires):
    return hmac.new(PDF_LINK_SECRET.encode(), f"respond:{filename}:{action}:{expires}".encode(), hashlib.sha256).hexdigest()[:16]


def verify_thread_action(filename, action, expires_str, sig):
    try:
        expires = int(expires_str)
    except (ValueError, TypeError):
        return False
    if time.time() > expires or action not in ("accept", "decline"):
        return False
    return hmac.compare_digest(sig or "", sign_thread_action(filename, action, expires))


@app.route("/")
def index():
    html = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>TxtAnOffer — Draft &amp; Verify TREC Contracts</title>
  <meta name="description" content="Texas agents and transaction coordinators: draft a TREC 20-19 by text message in 10 seconds, or drop any filled contract in and see what's missing before title kicks it back. No app required.">
  <link rel="icon" href="/static/favicon.ico" type="image/x-icon">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
  <style>
    :root {
      --bg: #F5F5F7;
      --card-dark: #0f1f2f;
      --card-dark-2: #152a3a;
      --card-dark-3: #112333;
      --bg-card: #fff;
      --border: rgba(15,31,47,0.08);
      --border-hover: rgba(23,23,23,0.35);
      --text: #0f1f2f;
      --text-muted: #5a6b7a;
      --text-dim: #8a9aa9;
      --accent: #171717;
      --accent-light: #525252;
      --accent-tint: #F0F0EE;
      --accent-glow: rgba(23,23,23,0.18);
      --radius: 1.25rem;
      --radius-sm: 0.85rem;
      --transition: all 0.2s ease;
    }

    * { margin: 0; padding: 0; box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background:
        radial-gradient(ellipse 90% 500px at 50% -80px, rgba(15,31,47,0.05) 0%, transparent 55%),
        radial-gradient(ellipse 50% 350px at 85% 100px, rgba(16,185,129,0.05) 0%, transparent 50%),
        var(--bg);
      color: var(--text);
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
      position: relative;
      overflow-x: hidden;
    }
    /* Ambient hero orbs -- attached to body (not .hero) so the glow bleeds to the
       real page edges instead of stopping at .main's 840px content width. */
    body::before {
      content: '';
      position: absolute;
      width: 400px; height: 400px;
      background: linear-gradient(135deg, rgba(15,31,47,0.10), rgba(15,31,47,0.05));
      border-radius: 50%;
      filter: blur(80px);
      top: -120px; right: -80px;
      pointer-events: none; z-index: 0;
    }
    body::after {
      content: '';
      position: absolute;
      width: 300px; height: 300px;
      background: linear-gradient(135deg, rgba(16,185,129,0.14), rgba(16,185,129,0.05));
      border-radius: 50%;
      filter: blur(70px);
      top: 260px; left: -80px;
      pointer-events: none; z-index: 0;
    }
    a { color: inherit; text-decoration: none; }

    /* Nav */
    .nav {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 1rem 2rem;
      position: sticky;
      top: 0;
      background: rgba(255,255,255,0.85);
      backdrop-filter: blur(20px);
      -webkit-backdrop-filter: blur(20px);
      border-bottom: 1px solid var(--border);
      z-index: 100;
    }
    .nav-left {
      display: flex;
      align-items: center;
      gap: 0.6rem;
      font-weight: 700;
      font-size: 1.1rem;
      letter-spacing: -0.02em;
    }
    .nav-logo {
      width: 34px; height: 34px;
      border-radius: 22%;
      overflow: hidden;
      background: var(--card-dark);
    }
    .nav-logo img {
      width: 100%; height: 100%;
      object-fit: contain;
    }
    .nav-links {
      display: flex;
      gap: 1.4rem;
      font-size: 0.85rem;
      font-weight: 500;
      color: var(--text-muted);
      flex-wrap: nowrap;
      white-space: nowrap;
    }
    .nav-links a { transition: var(--transition); }
    .nav-links a:hover { color: var(--text); }
    .nav-cta {
      background: rgba(15,31,47,0.06);
      color: var(--text);
      padding: 0.55rem 1.35rem;
      border-radius: 9999px;
      font-size: 0.875rem;
      font-weight: 600;
      border: 1px solid rgba(15,31,47,0.12);
      cursor: pointer;
      transition: var(--transition);
      display: inline-block;
      text-decoration: none;
    }
    .nav-cta:hover { background: rgba(15,31,47,0.1); }
    .nav-toggle { display: none; flex-direction: column; justify-content: center; gap: 5px; width: 34px; height: 34px; background: none; border: none; cursor: pointer; padding: 0; }
    .nav-toggle span { display: block; width: 100%; height: 2px; background: var(--text); border-radius: 2px; }

    /* Main column */
    .main { max-width: 840px; margin: 0 auto; padding: 0 2rem; position: relative; z-index: 1; }
    .section { padding-top: 4.5rem; }

    /* Hero */
    .hero { padding-top: 4rem; padding-bottom: 1rem; }
    .icon-circle {
      width: 52px; height: 52px; border-radius: 999px;
      background: var(--accent-tint); border: 1px solid rgba(23,23,23,0.18);
      display: flex; align-items: center; justify-content: center;
      margin-bottom: 1.5rem; color: var(--card-dark);
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      background: rgba(255,255,255,0.6);
      backdrop-filter: blur(6px);
      -webkit-backdrop-filter: blur(6px);
      border: 1px solid rgba(15,31,47,0.4);
      color: #000000;
      font-size: 0.7rem;
      font-weight: 700;
      padding: 0.35rem 0.85rem;
      border-radius: 9999px;
      width: fit-content;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      margin-bottom: 1.25rem;
    }
    .hero h1 {
      font-size: 3rem;
      font-weight: 800;
      line-height: 1.02;
      letter-spacing: -0.03em;
      max-width: 620px;
    }
    .hero-sub {
      margin-top: 1.1rem;
      font-size: 1.1rem;
      color: var(--text-muted);
      line-height: 1.6;
      max-width: 540px;
    }

    /* Input Card (real, functional) */
    .input-card {
      margin-top: 2rem;
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
      max-width: 540px;
    }
    .input-label {
      font-size: 0.7rem;
      font-weight: 700;
      color: var(--text-dim);
      text-transform: uppercase;
      letter-spacing: 0.07em;
    }
    .input-row { display: flex; gap: 0.5rem; }
    .input-row input {
      flex: 1;
      background: #F5F7F6;
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      box-shadow: 0 1px 3px rgba(15,31,47,0.03);
      padding: 0.8rem 1rem;
      color: var(--text);
      font-size: 0.95rem;
      font-family: inherit;
      outline: none;
      transition: var(--transition);
    }
    .input-row input:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(23,23,23,0.12);
      background: #fff;
    }
    .input-row input::placeholder { color: #a8b4bd; }
    .input-btn {
      background: var(--accent);
      color: #fff;
      border: 1px solid var(--accent);
      border-radius: var(--radius-sm);
      box-shadow: 0 4px 14px rgba(15,31,47,0.16);
      padding: 0.8rem 1.5rem;
      font-weight: 600;
      font-size: 0.9rem;
      font-family: inherit;
      cursor: pointer;
      transition: var(--transition);
      white-space: nowrap;
    }
    .input-btn:hover { background: var(--accent-light); border-color: var(--accent-light); }
    .input-hint { font-size: 0.75rem; color: var(--text-dim); }
    .hero-phone {
      margin-top: 0.85rem;
      padding-top: 0.85rem;
      border-top: 1px solid var(--border);
      font-size: 0.8rem;
      color: var(--text-muted);
    }
    .hero-phone a { color: #000000; font-weight: 600; text-decoration: none; }
    .hero-phone a:hover { text-decoration: underline; }

    /* Workflow strip -- Draft / Verify / Close */
    .workflow-strip { display: flex; align-items: center; gap: 0.6rem; margin-top: 1.5rem;
      font-size: 0.8rem; font-weight: 700; color: var(--text-muted); flex-wrap: wrap; }
    .workflow-step { display: flex; align-items: center; gap: 0.5rem; }
    .workflow-num { width: 22px; height: 22px; border-radius: 999px; background: var(--accent-tint);
      color: var(--text); display: flex; align-items: center; justify-content: center;
      font-size: 0.68rem; flex-shrink: 0; }
    .workflow-arrow { color: var(--text-dim); }

    /* TC-check upload widget (primary hero CTA) */
    .drop-zone { border: 2px dashed rgba(15,31,47,0.18); border-radius: var(--radius-sm); padding: 2rem 1.5rem;
      text-align: center; cursor: pointer; transition: var(--transition); background: #fff; }
    .drop-zone:hover, .drop-zone.drag { border-color: var(--accent); background: var(--accent-tint); }
    .drop-zone svg { margin-bottom: 0.6rem; }
    .drop-zone .dz-title { font-weight: 700; font-size: 0.95rem; margin-bottom: 0.2rem; }
    .drop-zone .dz-sub { color: var(--text-dim); font-size: 0.8rem; }
    .privacy-note { display: flex; align-items: center; gap: 0.45rem; margin-top: 0.9rem; font-size: 0.78rem; color: var(--text-dim); }
    .privacy-note svg { flex-shrink: 0; }
    input[type=file] { display: none; }
    .status { margin-top: 1rem; font-size: 0.85rem; color: var(--text-muted); display: none; }
    .status.show { display: block; }
    .result { margin-top: 1.25rem; display: none; }
    .result.show { display: block; }
    .result-banner { border-radius: var(--radius-sm); padding: 0.85rem 1.1rem; font-weight: 700; margin-bottom: 0.85rem; font-size: 0.9rem; }
    .result-banner.complete { background: rgba(16,185,129,0.1); border: 1px solid rgba(16,185,129,0.25); color: #047857; }
    .result-banner.incomplete { background: rgba(239,68,68,0.08); border: 1px solid rgba(239,68,68,0.2); color: #dc2626; }
    .issue-list { list-style: none; margin-bottom: 0.6rem; }
    .issue-item { display: flex; gap: 0.6rem; padding: 0.5rem 0; border-bottom: 1px solid var(--border); font-size: 0.85rem; }
    .issue-item:last-child { border-bottom: none; }
    .issue-tag { flex-shrink: 0; font-size: 0.62rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em;
      padding: 0.12rem 0.45rem; border-radius: 9999px; height: fit-content; }
    .issue-tag.blocker { background: rgba(239,68,68,0.12); color: #dc2626; }
    .issue-tag.warning { background: rgba(245,158,11,0.12); color: #b45309; }
    .result-more { font-size: 0.8rem; color: var(--text-muted); margin-top: 0.5rem; }
    .result-more a { color: var(--text); font-weight: 600; text-decoration: underline; }

    /* Secondary CTA (SMS draft) -- demoted below the primary check widget */
    .secondary-cta { margin-top: 1.75rem; padding-top: 1.5rem; border-top: 1px solid var(--border); max-width: 540px; }
    .secondary-cta-label { font-size: 0.85rem; color: var(--text-muted); margin-bottom: 0.75rem; }

    /* Stats -- glass card floating over the hero glow */
    .stats {
      display: flex; gap: 2.25rem; margin-top: 1.75rem; flex-wrap: wrap;
      background: rgba(255,255,255,0.65);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      border: 1px solid rgba(15,31,47,0.06);
      border-radius: 1.25rem;
      box-shadow: 0 1px 2px rgba(15,31,47,0.03), 0 8px 32px rgba(15,31,47,0.05);
      padding: 1.1rem 1.4rem;
      width: fit-content;
      max-width: 100%;
    }
    .stat-num { font-size: 1.4rem; font-weight: 800; color: var(--text); line-height: 1; }
    .stat-label { font-size: 0.72rem; color: var(--text-dim); margin-top: 0.25rem; font-weight: 500; }

    /* Dark card wrap (SMS demo + dashboard preview) -- layered ambient shadow */
    .dark-card-wrap {
      margin-top: 2.75rem;
      position: relative;
      border-radius: 2.25rem;
      background: var(--card-dark);
      padding: 10px;
      border: 1px solid rgba(255,255,255,0.08);
      box-shadow:
        0 2px 8px rgba(15,31,47,0.10),
        0 12px 40px rgba(15,31,47,0.14),
        0 24px 80px rgba(16,185,129,0.08);
      overflow: hidden;
    }
    .dark-card-inner {
      position: relative;
      border-radius: 1.75rem;
      background: linear-gradient(180deg, var(--card-dark-2) 0%, var(--card-dark-3) 50%, var(--card-dark) 100%);
      padding: 1.5rem;
      overflow: hidden;
    }
    .dark-card-inner::before {
      content: '';
      position: absolute;
      width: 320px; height: 320px;
      background: radial-gradient(circle, rgba(16,185,129,0.14) 0%, transparent 70%);
      top: -100px; left: 50%; transform: translateX(-50%);
      pointer-events: none; z-index: 0;
    }
    /* This card (the static "review screen" mockup) sits alone on a plain
       background with nothing else to blend into -- the green ambient touch
       above is only meant for the live SMS/PDF demo card, so suppress it here. */
    .dark-card-wrap.no-glow {
      box-shadow: 0 2px 8px rgba(15,31,47,0.10), 0 12px 40px rgba(15,31,47,0.14);
    }
    .dark-card-wrap.no-glow .dark-card-inner::before { display: none; }
    .notch {
      position: absolute; top: 0; left: 50%; transform: translateX(-50%);
      width: 88px; height: 20px;
      background: var(--card-dark);
      border-radius: 0 0 12px 12px;
      z-index: 1;
    }
    .sms-bubble {
      display: inline-flex; align-items: center; gap: 8px;
      border-radius: 999px; background: #fff; color: var(--text);
      padding: 0.6rem 1rem; font-size: 0.85rem; font-weight: 500;
      margin: 0.5rem auto 0; max-width: 100%;
    }
    .flow-arrow { text-align: center; color: rgba(255,255,255,0.35); font-size: 1.1rem; padding: 0.35rem 0; }
    .demo-wrap { max-width: 420px; margin: 0.5rem auto 0; position: relative; z-index: 1; }

    .demo-loading{display:none;color:var(--accent-light);font-size:0.85rem;padding:0.5rem 0;text-align:center;}
    .demo-error{display:none;color:#fca5a5;font-size:0.85rem;padding:0.5rem 0;text-align:center;}
    .white-card {
      background: #fff; border-radius: 1.1rem; padding: 1rem;
      border: 1px solid rgba(15,31,47,0.08);
    }
    .demo-result { display: block; }
    .demo-result.show { animation: cardPop 0.5s cubic-bezier(0.16,1,0.3,1) both; }
    @keyframes cardPop { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
    .demo-result .res-row {
      display: flex; justify-content: space-between; align-items: center;
      padding: 0.6rem 0.8rem; border-radius: 0.7rem;
      background: #fff; border: 1px solid rgba(15,31,47,0.06);
      box-shadow: 0 1px 2px rgba(15,31,47,0.05), 0 6px 16px -6px rgba(15,31,47,0.18);
      transition: opacity 0.35s ease, transform 0.35s cubic-bezier(0.16,1,0.3,1);
    }
    .demo-result .res-row + .res-row { margin-top: 0.5rem; }
    .demo-result .res-row .k { font-size: 0.68rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-dim); }
    .demo-result .res-row .v { font-size: 0.85rem; font-weight: 600; color: var(--text); }
    .pdf-card {
      display: flex; align-items: center; gap: 0.75rem;
      margin-top: 0.7rem; padding: 0.85rem; border-radius: 1rem;
      background: #fff; border: 1px solid rgba(15,31,47,0.06);
      box-shadow: 0 2px 4px rgba(15,31,47,0.06), 0 10px 26px -8px rgba(15,31,47,0.22);
      text-decoration: none;
      transition: opacity 0.35s ease, transform 0.35s cubic-bezier(0.16,1,0.3,1), box-shadow 0.2s ease;
    }
    .pdf-card:hover { transform: translateY(-2px); box-shadow: 0 4px 8px rgba(15,31,47,0.08), 0 16px 34px -8px rgba(15,31,47,0.28); }
    .pdf-icon {
      width: 40px; height: 40px; border-radius: 0.7rem; flex-shrink: 0;
      background: var(--accent-tint); display: flex; align-items: center; justify-content: center;
    }
    .pdf-meta { min-width: 0; }
    .pdf-title { font-size: 0.83rem; font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .pdf-sub { font-size: 0.72rem; color: var(--text-dim); margin-top: 0.1rem; }

    /* Illustrative demo animation (typing + staggered reveal) */
    .sms-cursor { display: inline-block; width: 2px; height: 1em; margin-left: 2px; vertical-align: -2px; background: currentColor; opacity: 0; }
    .sms-cursor.blink { animation: smsCursorBlink 0.9s steps(1) infinite; }
    @keyframes smsCursorBlink { 50% { opacity: 0; } 0%, 100% { opacity: 1; } }
    .res-row .v, #pdf-flow-arrow, #res-pdf { transition: opacity 0.35s ease, transform 0.35s cubic-bezier(0.16,1,0.3,1); }
    .res-row .v { display: inline-block; }
    @media (prefers-reduced-motion: reduce) {
      .sms-cursor.blink { animation: none; opacity: 0; }
    }

    /* Steps */
    .steps { max-width: 1000px; margin: 0 auto; padding: 4.5rem 2rem; border-top: 1px solid var(--border); }
    .steps-header { text-align: center; margin-bottom: 3rem; }
    .steps-kicker { font-size: 0.7rem; font-weight: 700; color: var(--text-dim); text-transform: uppercase;
      letter-spacing: 0.07em; margin-bottom: 0.6rem; }
    .steps-header h2 { font-size: 2rem; font-weight: 800; margin: 0 0 0.5rem; letter-spacing: -0.02em; }
    .steps-header p { color: var(--text-dim); font-size: 1rem; }
    .steps-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1.25rem; }
    @media (min-width: 961px) {
      #how .steps-grid { grid-template-columns: repeat(4, 1fr); }
    }
    .step-card {
      background: #fff;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1.75rem;
      transition: transform 0.25s cubic-bezier(0.16,1,0.3,1), box-shadow 0.25s ease, border-color 0.25s ease;
    }
    .step-card:hover {
      border-color: var(--border-hover);
      transform: translateY(-4px);
      box-shadow: 0 4px 8px rgba(15,31,47,0.05), 0 16px 32px -8px rgba(15,31,47,0.14);
    }
    .step-num {
      width: 38px; height: 38px;
      background: var(--accent-tint);
      color: #000000;
      border-radius: var(--radius-sm);
      display: flex; align-items: center; justify-content: center;
      font-weight: 700;
      font-size: 0.85rem;
      margin-bottom: 1.1rem;
    }
    .step-card h3 { font-size: 1.05rem; font-weight: 700; margin: 0 0 0.5rem; letter-spacing: -0.01em; }
    .step-card p { font-size: 0.87rem; color: var(--text-muted); line-height: 1.55; margin: 0; }
    .step-caption { font-size: 0.75rem; color: var(--text-dim); margin-top: 0.5rem; }

    /* Dashboard preview (Connected Apps + review-screen mockup) */
    .dash-grid { display: grid; grid-template-columns: 1fr; gap: 0.85rem; padding: 0.5rem; }
    @media (min-width: 700px) { .dash-grid { grid-template-columns: 260px 1fr; } }
    .dash-panel { background: #fff; border-radius: 1.1rem; padding: 1.1rem; }
    .dash-panel-label {
      font-size: 0.68rem; font-weight: 700; letter-spacing: 0.07em; text-transform: uppercase;
      color: var(--text-dim); display: flex; align-items: center; justify-content: space-between;
    }
    .integration-row { display: flex; align-items: center; gap: 0.65rem; padding: 0.6rem 0; }
    .integration-row:not(:last-of-type) { border-bottom: 1px solid var(--border); }
    .integration-icon {
      width: 28px; height: 28px; border-radius: 8px; background: var(--accent-tint);
      display: flex; align-items: center; justify-content: center; font-size: 0.7rem; font-weight: 700; color: var(--card-dark); flex-shrink: 0;
    }
    .integration-name { font-size: 0.82rem; font-weight: 500; color: var(--text); }
    .integration-note { font-size: 0.75rem; color: var(--text-dim); line-height: 1.5; margin-top: 0.75rem; }
    .chrome-bar {
      display: flex; align-items: center; gap: 6px;
      padding-bottom: 0.85rem; margin-bottom: 0.9rem; border-bottom: 1px solid var(--border);
    }
    .chrome-dot { width: 9px; height: 9px; border-radius: 999px; background: #e2e6e5; }
    .chrome-title { font-size: 0.72rem; color: var(--text-dim); font-weight: 500; margin-left: 0.4rem; }
    .review-address { font-size: 1rem; font-weight: 700; }
    .review-sub { font-size: 0.72rem; color: var(--text-dim); margin-top: 0.1rem; }
    .review-stats { display: flex; gap: 0.6rem; margin: 0.85rem 0; flex-wrap: wrap; }
    .review-stat { background: rgba(15,31,47,0.04); border-radius: 0.7rem; padding: 0.5rem 0.75rem; flex: 1; min-width: 100px; }
    .review-stat .k { font-size: 0.62rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-dim); }
    .review-stat .v { font-size: 0.85rem; font-weight: 700; margin-top: 0.1rem; }
    .review-warning {
      background: rgba(15,31,47,0.04); border: 1px solid var(--border); color: var(--text-muted);
      border-radius: 0.7rem; padding: 0.65rem 0.85rem; font-size: 0.78rem; line-height: 1.45; margin-bottom: 0.85rem;
    }
    .review-actions { display: flex; gap: 0.6rem; flex-wrap: wrap; }
    .review-btn {
      flex: 1; min-width: 140px; text-align: center; padding: 0.65rem; border-radius: 999px;
      font-size: 0.8rem; font-weight: 600;
    }
    .review-btn.primary { background: var(--card-dark); color: #fff; }
    .review-btn.ghost { background: rgba(15,31,47,0.05); color: var(--text-muted); }
    .review-caption { font-size: 0.72rem; color: var(--text-dim); margin-top: 0.75rem; text-align: center; }

    /* Footer */
    .footer { border-top: 1px solid var(--border); padding: 3rem 2rem; text-align: center; }
    .footer-links { display: flex; justify-content: center; gap: 1.5rem; margin-bottom: 1rem; flex-wrap: wrap; }
    .footer-links a { color: var(--text-dim); font-size: 0.85rem; font-weight: 500; transition: var(--transition); }
    .footer-links a:hover { color: var(--text); }
    .trust-badges { display: flex; justify-content: center; align-items: center; gap: 1.5rem; margin-bottom: 1.25rem; flex-wrap: wrap; }
    .trust-badge { display: inline-flex; align-items: center; gap: 0.4rem; font-size: 0.76rem; font-weight: 600; color: var(--text-dim); }
    .trust-badge .trust-icon { font-size: 0.9rem; }
    .footer-copy { color: var(--text-dim); font-size: 0.8rem; }

    @media (max-width: 700px) {
      /* The ambient orbs/gradient are fixed-px sized, tuned to read as a subtle
         accent against a wide desktop viewport -- at mobile widths those same
         pixel sizes cover most of the screen and read as a dominant wash
         instead, so scale everything down here. */
      body {
        background:
          radial-gradient(ellipse 90% 260px at 50% -60px, rgba(15,31,47,0.05) 0%, transparent 55%),
          radial-gradient(ellipse 70% 180px at 85% 60px, rgba(16,185,129,0.05) 0%, transparent 50%),
          var(--bg);
      }
      body::before { width: 200px; height: 200px; top: -60px; right: -50px; filter: blur(50px); }
      body::after { width: 150px; height: 150px; top: 140px; left: -50px; filter: blur(45px); }
      .main { padding: 0 1.25rem; }
      .hero h1 { font-size: 2.25rem; }
      .steps-grid { grid-template-columns: 1fr; }
      .nav-toggle { display: flex; }
      .nav-links {
        display: none; position: absolute; top: 100%; left: 0; right: 0;
        flex-direction: column; gap: 0; padding: 0.5rem 1.25rem 1.25rem;
        background: #fff; border-bottom: 1px solid rgba(15,31,47,0.08);
        white-space: normal;
      }
      .nav-links.open { display: flex; }
      .nav-links a { padding: 0.75rem 0; border-bottom: 1px solid rgba(15,31,47,0.08); }
      .nav-links a:last-child { border-bottom: none; }
      .stats { gap: 1.5rem; }
    }
    @media (max-width: 480px) {
      .hero h1 { font-size: 1.9rem; }
      .input-row { flex-direction: column; }
      .input-btn { width: 100%; }
      .nav { padding: 1rem; }
    }
  </style>
</head>
<body>

  <nav class="nav">
    <a href="/" class="nav-left">
      <div class="nav-logo"><img src="/static/logo.svg" alt="TxtAnOffer"></div>
      <span>TxtAnOffer</span>
    </a>
    <div class="nav-links" id="navLinks">
      <a href="#how">How it works</a>
      <a href="#trust">Accuracy</a>
      <a href="/pricing">Pricing</a>
      <a href="/demo">Demo</a>
      <a href="/playground">Parser Playground</a>
      <a href="/tc-check">TC File Check</a>
      <a href="/faq">FAQ</a>
      <a href="/about">About</a>
      <a href="/trec-changes">TREC Changes</a>
      <a href="/contact">Support</a>
      <a href="/login">Log In</a>
    </div>
    <a href="/signup" class="nav-cta">Start Free Trial</a>
    <button class="nav-toggle" id="navToggle" aria-label="Menu" aria-expanded="false"><span></span><span></span><span></span></button>
  </nav>
  <script>
  (function(){
    var t=document.getElementById('navToggle'), l=document.getElementById('navLinks');
    if(!t||!l) return;
    t.addEventListener('click', function(){
      var open = l.classList.toggle('open');
      t.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    l.querySelectorAll('a').forEach(function(a){
      a.addEventListener('click', function(){ l.classList.remove('open'); t.setAttribute('aria-expanded','false'); });
    });
  })();
  </script>

  <div class="main">
  <section class="hero section">
    <div class="badge">For Texas Agents &amp; Transaction Coordinators</div>
    <h1>
      Draft instantly.<br>
      Verify completely.<br>
      Close smoothly.
    </h1>
    <p class="hero-sub">
      TxtAnOffer drafts your <strong>TREC 20-19</strong> by text message, and checks any filled contract &mdash; yours or anyone else's &mdash; for the missing initials, dates, and mismatches that make title kick a file back.
    </p>

    <div class="workflow-strip">
      <div class="workflow-step"><span class="workflow-num">1</span>Draft</div>
      <div class="workflow-arrow">&rarr;</div>
      <div class="workflow-step"><span class="workflow-num">2</span>Verify</div>
      <div class="workflow-arrow">&rarr;</div>
      <div class="workflow-step"><span class="workflow-num">3</span>Close</div>
    </div>

    <div class="input-card">
      <div class="input-label">Try it now &mdash; no signup required</div>
      <div class="drop-zone" id="homeDropZone">
        <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="#8a9aa9" stroke-width="1.5"><path d="M12 16V4M12 4l-4 4M12 4l4 4" stroke-linecap="round" stroke-linejoin="round"/><path d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        <div class="dz-title">Drop a filled TREC 20-19 PDF here, or click to choose</div>
        <div class="dz-sub">We'll tell you what's missing before title kicks it back.</div>
      </div>
      <input type="file" id="homeFileInput" accept="application/pdf">
      <div class="privacy-note"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>Processed instantly and never stored &mdash; discarded the moment your results are ready.</div>
      <div class="status" id="homeStatus"></div>
      <div class="result" id="homeResult"></div>
    </div>
  </section>

  <section class="hero section" style="padding-top:1rem;">
    <h1 style="font-size:2.1rem;">See exactly what you'll get</h1>
    <p class="hero-sub">The same review screen every offer goes through before it can be sent &mdash; and where DocuSign and Zapier plug in.</p>

    <div class="dark-card-wrap no-glow">
      <div class="dark-card-inner" style="padding:0.6rem;">
        <div class="dash-grid">
          <div class="dash-panel">
            <div class="dash-panel-label">Integrations</div>
            <div style="margin-top:0.85rem;">
              <div class="integration-row">
                <div class="integration-icon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20c3-1 4-4 6-8s4-8 6-8"/><path d="M14 20c2-3 4-4 6-4"/></svg></div>
                <span class="integration-name">DocuSign</span>
              </div>
              <div class="integration-row">
                <div class="integration-icon"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M13 2 3 14h7l-1 8 11-13h-8z"/></svg></div>
                <span class="integration-name">Zapier / Webhooks</span>
              </div>
            </div>
            <div class="integration-note">Included on the Professional plan &mdash; send to DocuSign or POST to any URL right from the review screen.</div>
          </div>
          <div class="dash-panel">
            <div class="chrome-bar">
              <div class="chrome-dot"></div><div class="chrome-dot"></div><div class="chrome-dot"></div>
              <span class="chrome-title">txtanoffer.com/review</span>
            </div>
            <div class="review-address">123 Main St</div>
            <div class="review-sub">TREC One to Four Family Residential Contract</div>
            <div class="review-stats">
              <div class="review-stat"><div class="k">Price</div><div class="v">$725,000</div></div>
              <div class="review-stat"><div class="k">Down</div><div class="v">3% ($21,750)</div></div>
              <div class="review-stat"><div class="k">Close</div><div class="v">21 days</div></div>
            </div>
            <div class="review-warning">Heads up: Section 1 &mdash; Buyer and/or Seller legal name is blank. Fill in by hand before sending.</div>
            <div class="review-actions">
              <div class="review-btn primary">Email to Listing Agent</div>
              <div class="review-btn ghost">Open PDF</div>
            </div>
            <div class="review-caption">Nothing sends until every required field is filled in.</div>
          </div>
        </div>
      </div>
    </div>
  </section>
  </div>

  <section class="steps" id="how">
    <div class="steps-header">
      <div class="steps-kicker">How the SMS drafting engine works</div>
      <h2>Four steps. No app required.</h2>
      <p>Works with any phone that can send a text message.</p>
    </div>
    <div class="steps-grid">
      <div class="step-card">
        <div class="step-num">01</div>
        <h3>Sign Up</h3>
        <p>Register your phone and agent details. Get a confirmation text to opt in to our SMS service.</p>
      </div>
      <div class="step-card">
        <div class="step-num">02</div>
        <h3>Text Your Offer</h3>
        <p>Send price, down payment %, closing days, and address. Our parser extracts everything automatically.</p>
      </div>
      <div class="step-card">
        <div class="step-num">03</div>
        <h3>Get Your PDF</h3>
        <p>Receive a link to your filled TREC contract + financing addendum in seconds, ready for DocuSign.</p>
      </div>
      <div class="step-card">
        <div class="step-num">04</div>
        <h3>Get Their Answer</h3>
        <p>Emailing the listing agent sends them a link with the same PDF, plus Accept and Decline buttons. The moment they respond, you get a text &mdash; no more wondering if your offer even got read.</p>
        <p class="step-caption">Non-binding intent only &mdash; formal acceptance still requires normal signing of the TREC 20-19.</p>
      </div>
    </div>

    <div class="secondary-cta" style="margin:2.5rem auto 0;padding-top:0;border-top:none;max-width:540px;text-align:center;">
      <div class="secondary-cta-label">Try step 2 yourself — text your details to generate a flawless draft in 60 seconds.</div>
      <form id="live-demo-form">
        <div class="input-row">
          <input type="text" id="demo-input" placeholder="725k 3% 21day Harris 1234 Westheimer Rd" autocomplete="off">
          <button type="submit" class="input-btn">Generate &rarr;</button>
        </div>
      </form>
      <div class="input-hint">Type however feels natural — we handle messy texts. Just get the numbers in there.</div>
      <div class="hero-phone">Prefer texting from your phone? <a href="sms:+18338970333">Text (833) 897-0333</a> to get started.</div>
      <div class="input-hint" style="margin-top:0.4rem;">By texting, you agree to receive automated messages from TxtAnOffer. Reply STOP to opt out, HELP for help. Msg &amp; data rates may apply.</div>
    </div>

    <div class="stats" style="max-width:640px;margin-left:auto;margin-right:auto;">
      <div><div class="stat-num">&lt;10s</div><div class="stat-label">Generation time</div></div>
      <div><div class="stat-num">45 min</div><div class="stat-label">Saved per offer</div></div>
      <div><div class="stat-num">Free</div><div class="stat-label">No card required</div></div>
      <div><div class="stat-num">100%</div><div class="stat-label">Required fields checked</div></div>
    </div>

    <div class="dark-card-wrap" style="max-width:400px;margin:1.75rem auto 0;">
      <div class="dark-card-inner">
        <div class="notch"></div>
        <div class="demo-wrap">
          <div class="sms-bubble" style="display:flex;"><span id="sms-typed">725k 3% 21day 123 Main St</span><span class="sms-cursor" id="sms-cursor"></span></div>
          <div class="flow-arrow">&darr;</div>
          <div class="demo-loading" id="demo-loading">Generating your contract...</div>
          <div class="demo-error" id="demo-error"></div>
          <div class="demo-result" id="demo-result">
            <div class="white-card">
              <div class="res-row"><span class="k">Address</span><span class="v" id="res-addr">123 Main St</span></div>
              <div class="res-row"><span class="k">Price</span><span class="v" id="res-price">$725,000</span></div>
              <div class="res-row"><span class="k">Down payment</span><span class="v" id="res-down">3%</span></div>
              <div class="res-row"><span class="k">Closing</span><span class="v" id="res-close">21 days</span></div>
            </div>
            <div class="flow-arrow" id="pdf-flow-arrow">&darr;</div>
            <a href="#" id="res-pdf" class="pdf-card" target="_blank">
              <div class="pdf-icon">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#000000" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
              </div>
              <div class="pdf-meta">
                <div class="pdf-title">TREC 20-19 Purchase Agreement.pdf</div>
                <div class="pdf-sub">AES-256 encrypted &middot; Ready to sign</div>
              </div>
            </a>
          </div>
        </div>
      </div>
    </div>
  </section>

  <section class="steps" id="trust">
    <div class="steps-header">
      <h2>Built so nothing slips through.</h2>
      <p>The anxiety isn't "I wish this were faster" &mdash; it's "did I miss a checkbox." Here's how we handle that.</p>
    </div>
    <div class="steps-grid">
      <div class="step-card">
        <div class="step-num">&check;</div>
        <h3>Every field checked, not just assumed</h3>
        <p>We re-read the finished PDF itself &mdash; not just the code that filled it &mdash; and flag exactly which required field, checkbox, or dollar amount is missing right on your review screen, before you send it to anyone. Emailing straight to the listing agent is blocked outright until everything required is filled in.</p>
      </div>
      <div class="step-card">
        <div class="step-num">&check;</div>
        <h3>Built on TREC's current form</h3>
        <p>Generated from TREC's actual published 20-19 form &mdash; current Paragraph 12B commission language, the mandatory Water Disclosure, and the required IABS brokerage-services notice all included &mdash; not a stale template someone forgot to update. Mention an HOA and the 36-10 addendum attaches itself, checkbox and all &mdash; no separate form to remember. <a href="/trec-changes" style="color:var(--text);text-decoration:underline;">See what changed &rarr;</a></p>
      </div>
      <div class="step-card">
        <div class="step-num">&check;</div>
        <h3>You review it. You send it.</h3>
        <p>TxtAnOffer drafts the contract; nothing goes to a buyer, seller, or listing agent until you look it over and decide it's ready.</p>
      </div>
    </div>
  </section>

  <footer class="footer">
    <div class="trust-badges">
      <span class="trust-badge"><span class="trust-icon">&#128274;</span>AES-256 Encrypted</span>
      <span class="trust-badge"><span class="trust-icon">&#9729;</span>SOC 2 Type II Infrastructure</span>
      <span class="trust-badge"><span class="trust-icon">&#128179;</span>Billing by Stripe</span>
    </div>
    <div class="footer-links">
      <a href="/about">About</a>
      <a href="/faq">FAQ</a>
      <a href="/contact">Contact</a>
      <a href="/terms">Terms of Service</a>
      <a href="/privacy">Privacy Policy</a>
      <a href="/privacy#sms-messaging">SMS Terms</a>
      <a href="/pricing">Pricing</a>
      <a href="/playground">Parser Playground</a>
      <a href="/tc-check">TC File Check</a>
      <a href="mailto:support@txtanoffer.com">Support</a>
    </div>
    <div class="footer-copy">
      &copy; 2026 TxtAnOffer &middot; Operated by Phanel &middot; Texas, United States &middot; Not affiliated with TREC
    </div>
  </footer>

<script>
(function(){
  var form=document.getElementById('live-demo-form'),
      input=document.getElementById('demo-input'),
      loading=document.getElementById('demo-loading'),
      errEl=document.getElementById('demo-error'),
      result=document.getElementById('demo-result');
  form.addEventListener('submit',function(e){
    e.preventDefault();
    var text=input.value.trim();
    if(!text)return;
    loading.style.display='block';
    errEl.style.display='none';
    result.classList.remove('show');
    var typedEl=document.getElementById('sms-typed'),
        addrEl=document.getElementById('res-addr'),
        priceEl=document.getElementById('res-price'),
        downEl=document.getElementById('res-down'),
        closeEl=document.getElementById('res-close'),
        pdfArrow=document.getElementById('pdf-flow-arrow'),
        pdfCard=document.getElementById('res-pdf');
    // Show the visitor's own text immediately -- the illustrative loop's
    // frozen "123 Main St" example must never linger once a real
    // submission is in flight, or the SMS bubble contradicts the result.
    if(typedEl) typedEl.textContent=text;
    fetch('/api/demo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({offer_text:text})})
    .then(function(r){return r.json()})
    .then(function(d){
      loading.style.display='none';
      if(d.error){
        errEl.textContent=d.error;errEl.style.display='block';
        // Clear stale illustrative values so the error doesn't sit next
        // to a fake address/price/PDF that was never actually generated.
        addrEl.textContent=''; priceEl.textContent=''; downEl.textContent=''; closeEl.textContent='';
        if(pdfArrow) pdfArrow.style.opacity='0';
        if(pdfCard) pdfCard.style.opacity='0';
        return;
      }
      addrEl.textContent=d.address;
      priceEl.textContent='$'+Number(d.price).toLocaleString();
      downEl.textContent=d.down_pct+'%';
      closeEl.textContent=d.close_date;
      document.getElementById('res-pdf').href=d.pdf_url;
      if(pdfArrow) pdfArrow.style.opacity='1';
      if(pdfCard) pdfCard.style.opacity='1';
      result.classList.add('show');
    })
    .catch(function(){loading.style.display='none';errEl.textContent='Something went wrong. Try again.';errEl.style.display='block';});
  });
})();

// Illustrative example above: types out the sample SMS once on page
// load, then reveals Address/Price/Down/Closing one at a time, then
// the PDF card -- purely decorative, plays exactly once and then sits
// on the finished state. Independent of the real /api/demo form above;
// a real submission stops it for good so it can never clobber a real
// result the visitor is looking at.
(function(){
  var typedEl = document.getElementById('sms-typed'),
      cursorEl = document.getElementById('sms-cursor'),
      addrEl = document.getElementById('res-addr'),
      priceEl = document.getElementById('res-price'),
      downEl = document.getElementById('res-down'),
      closeEl = document.getElementById('res-close'),
      pdfArrow = document.getElementById('pdf-flow-arrow'),
      pdfCard = document.getElementById('res-pdf'),
      demoForm = document.getElementById('live-demo-form');
  if(!typedEl || !addrEl) return;

  var SCRIPT_TEXT = '725k 3% 21day 123 Main St';
  var VALUES = {addr:'123 Main St', price:'$725,000', down:'3%', close:'21 days'};
  var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var stopped = false, timers = [];

  function clearTimers(){ timers.forEach(function(t){clearTimeout(t);}); timers = []; }
  function after(ms, fn){ timers.push(setTimeout(fn, ms)); }
  function fade(el, show){
    if(!el) return;
    el.style.opacity = show ? '1' : '0';
    el.style.transform = show ? 'translateY(0) scale(1)' : 'translateY(8px) scale(0.96)';
  }

  function typeText(el, text, cb){
    var i = 0;
    el.textContent = '';
    (function step(){
      if(stopped) return;
      el.textContent = text.slice(0, i);
      i++;
      if(i <= text.length){ after(45, step); } else if(cb){ after(200, cb); }
    })();
  }

  function playCycle(){
    if(stopped || reduceMotion) return;
    fade(addrEl,false); fade(priceEl,false); fade(downEl,false); fade(closeEl,false);
    fade(pdfArrow,false); fade(pdfCard,false);
    addrEl.textContent=''; priceEl.textContent=''; downEl.textContent=''; closeEl.textContent='';
    cursorEl.classList.add('blink');
    typeText(typedEl, SCRIPT_TEXT, function(){
      cursorEl.classList.remove('blink');
      after(300, function(){
        addrEl.textContent = VALUES.addr; fade(addrEl, true);
        after(280, function(){
          priceEl.textContent = VALUES.price; fade(priceEl, true);
          after(280, function(){
            downEl.textContent = VALUES.down; fade(downEl, true);
            after(280, function(){
              closeEl.textContent = VALUES.close; fade(closeEl, true);
              after(450, function(){
                fade(pdfArrow, true);
                after(200, function(){
                  fade(pdfCard, true);
                });
              });
            });
          });
        });
      });
    });
  }

  if(!reduceMotion){ after(700, playCycle); }

  if(demoForm){
    demoForm.addEventListener('submit', function(){
      stopped = true;
      clearTimers();
      cursorEl.classList.remove('blink');
      [addrEl,priceEl,downEl,closeEl,pdfArrow,pdfCard].forEach(function(el){ if(el) el.style.opacity=''; });
    });
  }
})();
</script>
<script>
(function(){
  var dropZone = document.getElementById('homeDropZone'),
      fileInput = document.getElementById('homeFileInput'),
      statusEl = document.getElementById('homeStatus'),
      resultEl = document.getElementById('homeResult');
  if(!dropZone) return;

  dropZone.addEventListener('click', function(){ fileInput.click(); });
  dropZone.addEventListener('dragover', function(e){ e.preventDefault(); dropZone.classList.add('drag'); });
  dropZone.addEventListener('dragleave', function(){ dropZone.classList.remove('drag'); });
  dropZone.addEventListener('drop', function(e){
    e.preventDefault();
    dropZone.classList.remove('drag');
    if(e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]);
  });
  fileInput.addEventListener('change', function(){
    if(fileInput.files.length) uploadFile(fileInput.files[0]);
  });

  // Same perceived-progress pattern as /tc-check -- one real round trip,
  // staged labels just so the wait doesn't feel dead.
  var STATUS_STEPS = ['Reading PDF...', 'Checking required fields...', 'Checking initials & consistency...'];
  var statusTimers = [];

  function uploadFile(file){
    resultEl.classList.remove('show');
    statusTimers.forEach(clearTimeout);
    statusTimers = STATUS_STEPS.map(function(label, i){
      return setTimeout(function(){ statusEl.textContent = label; }, i * 450);
    });
    statusEl.textContent = STATUS_STEPS[0];
    statusEl.classList.add('show');

    var formData = new FormData();
    formData.append('file', file);

    fetch('/v1/tc/check', { method: 'POST', body: formData })
      .then(function(r){ return r.json(); })
      .then(function(data){
        statusTimers.forEach(clearTimeout);
        statusEl.classList.remove('show');
        if(data.error){ renderError(data.error); return; }
        if(data.email_required){ renderEmailPrompt(data); return; }
        renderResult(data);
      })
      .catch(function(){
        statusTimers.forEach(clearTimeout);
        statusEl.classList.remove('show');
        renderError('Something went wrong checking that file. Try again.');
      });
  }

  function renderError(msg){
    resultEl.innerHTML = '<div class="result-banner incomplete">' + escapeHtml(msg) + '</div>';
    resultEl.classList.add('show');
  }

  // Homepage widget keeps the email-capture gate simple: rather than
  // duplicating the email form here, show the real issue count (that's
  // the hook) and send them to /tc-check to unlock the itemized list.
  function renderEmailPrompt(data){
    var html = '<div class="result-banner incomplete">' + data.issue_count + ' issue' + (data.issue_count === 1 ? '' : 's') + ' found</div>';
    html += '<div class="result-more"><a href="/tc-check">Enter your email on TC File Check to see the full itemized report &rarr;</a></div>';
    resultEl.innerHTML = html;
    resultEl.classList.add('show');
  }

  function renderResult(data){
    var issues = data.issues || [];
    var html = '';
    if(data.complete){
      html += '<div class="result-banner complete">All checked fields are filled in.</div>';
    } else {
      html += '<div class="result-banner incomplete">' + issues.length + ' issue' + (issues.length === 1 ? '' : 's') + ' found</div>';
    }
    // Homepage widget shows the first few issues -- the full checklist,
    // copy/download buttons, and blank-draft CTA live on /tc-check itself.
    var shown = issues.slice(0, 4);
    if(shown.length){
      html += '<ul class="issue-list">';
      shown.forEach(function(issue){
        html += '<li class="issue-item"><span class="issue-tag ' + issue.severity + '">' + issue.severity + '</span><span>' + escapeHtml(issue.message) + '</span></li>';
      });
      html += '</ul>';
    }
    if(issues.length > shown.length){
      html += '<div class="result-more">+' + (issues.length - shown.length) + ' more &mdash; <a href="/tc-check">see the full checklist &rarr;</a></div>';
    } else if(issues.length){
      html += '<div class="result-more"><a href="/tc-check">Copy or download this checklist &rarr;</a></div>';
    }
    resultEl.innerHTML = html;
    resultEl.classList.add('show');
  }

  function escapeHtml(s){
    var div = document.createElement('div');
    div.textContent = s;
    return div.innerHTML;
  }
})();
</script>
</body>
</html>
"""
    # Carry ?src=name (e.g. from a Direct Reach email) through the "Start
    # Free Trial" CTA to /signup, so the eventual signup still attributes
    # correctly even though the link lands on the homepage first -- see
    # get_signups_by_source() on /analytics.
    import re as _re
    src = _re.sub(r"[^a-zA-Z0-9_-]", "", request.args.get("src", ""))[:60]
    if src:
        html = html.replace(
            '<a href="/signup" class="nav-cta">Start Free Trial</a>',
            f'<a href="/signup?src={src}" class="nav-cta">Start Free Trial</a>',
            1,
        )
    resp = make_response(html)
    # First-touch attribution cookie: the query-param rewrite above only
    # survives if the visitor clicks "Start Free Trial" in this exact page
    # load. Cold-outreach signups routinely happen on a later visit (a
    # different page, a different day) with no ?src on that later click,
    # which silently misattributes real Direct Reach conversions as
    # "direct". A 30-day first-touch cookie, read as a fallback in
    # signup(), fixes that -- set only once so a later plain "/" visit
    # can't overwrite genuine attribution with "direct".
    if src and not request.cookies.get("ta_src"):
        resp.set_cookie("ta_src", src, max_age=30 * 24 * 3600, httponly=True, samesite="Lax")
    # Log the raw click regardless of whether it ever converts -- signups-by-
    # source alone can't tell "nobody opened the link" apart from "people
    # opened it and left", since both are silence in that table. Only log
    # first-touch (no ta_src cookie yet) so repeat visits from the same
    # browser during the same 30-day window don't inflate the count.
    if src and not request.cookies.get("ta_src"):
        track_event("landing_visit", None, {"source": src})
    return resp


# --- address validation --------------------------------------------------
# Fast, dependency-free sanity check on the parsed address before it goes
# anywhere near a legal contract. NOT full USPS/geocoding validation -- it
# catches the most common parser failures (missing street number, missing
# street suffix) before they get silently baked into a PDF.
import re as _re

_STREET_SUFFIXES = r"""(?:
    st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|ln|lane|
    ct|court|way|pl|place|cir|circle|ter|terrace|pkwy|parkway|
    hwy|highway|trl|trail|loop|xing|crossing|sq|square|walk
)"""
_STREET_SUFFIX_RE = _re.compile(r"\b\d+\b.*\b" + _STREET_SUFFIXES + r"\b\.?", _re.IGNORECASE | _re.VERBOSE)
_STREET_NUMBER_RE = _re.compile(r"^\s*\d{1,6}\b")
_TX_ZIP_RE = _re.compile(r"\b7[0-9]{4}\b")
_STATE_RE = _re.compile(r"\bTX\b|\btexas\b", _re.IGNORECASE)

# Explicit non-Texas state signal -- distinct from _STATE_RE above, which only
# checks whether TX/Texas is present. This checks whether ANOTHER state is
# named, so an address like "Long Beach, CA" gets hard-blocked instead of
# falling into the soft TX-unverified quick-choice (where an agent could just
# reply "1 = yes, Texas" and force a TREC contract onto a non-Texas
# property -- a real liability issue, not just a data-quality one).
# Abbreviations are only matched in clear address position (", ST" or "ST
# 12345") to avoid firing on common words that double as state codes (IN, OR,
# OK, HI, ME, PA...).
_OTHER_STATE_ABBRS = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|"
    "MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|UT|VT|VA|WA|"
    "WV|WI|WY|DC"
)
_OTHER_STATE_ABBR_RE = _re.compile(
    r",\s*(" + _OTHER_STATE_ABBRS + r")\b|\b(" + _OTHER_STATE_ABBRS + r")\s+\d{5}\b"
)
_OTHER_STATE_NAMES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|"
    "florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|"
    "louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|"
    "missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|"
    "new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|"
    "rhode island|south carolina|south dakota|tennessee|utah|vermont|virginia|"
    "washington|west virginia|wisconsin|wyoming"
)
_OTHER_STATE_NAME_RE = _re.compile(r"\b(" + _OTHER_STATE_NAMES + r")\b", _re.IGNORECASE)

# Internal detection string only -- matched against validate_address()'s
# warnings list to decide whether to show the TX-confirmation quick-choice.
# The actual SMS copy shown to the agent is deliberately softer (see the "1"
# reply handler below); this text never goes out in a message.
TX_UNVERIFIED_WARNING = "We couldn't verify that this property is in Texas. Please confirm before generating the final contract."


def validate_address(address: str, raw_text: str = None) -> dict:
    """
    raw_text: the original, unparsed message text, used only for the TX/state
    check. The cleaned street address never contains "TX" or a zip code --
    parser.py's _parse_address() deliberately strips them out to isolate the
    street portion -- so checking `address` itself for state signal would
    always fail, flagging every offer as unverified regardless of what the
    agent actually typed. Falls back to `address` if raw_text isn't given.

    Returns:
        {"valid": bool, "reason": str|None, "warnings": list[str],
         "normalized": str, "other_state": str|None}
        other_state is set only when the block is a confirmed non-Texas
        state (not the other invalid reasons like a missing street number)
        -- callers use it to route to the waitlist-invite message/capture
        instead of a flat refusal.
    """
    result = {"valid": False, "reason": None, "warnings": [], "normalized": "", "other_state": None}

    if not address or not address.strip():
        result["reason"] = "No address found in the message."
        return result

    cleaned = _re.sub(r"\s+", " ", address.strip())
    result["normalized"] = cleaned

    if not _STREET_NUMBER_RE.search(cleaned):
        result["reason"] = (
            f'"{cleaned}" doesn\'t start with a street number. '
            f"Include the full address, e.g. 123 Main St."
        )
        return result

    if not _STREET_SUFFIX_RE.search(cleaned):
        result["reason"] = (
            f'"{cleaned}" is missing a recognizable street type '
            f"(St, Ave, Rd, Blvd, Dr, Ln, etc). Double check the address."
        )
        return result

    state_check_text = raw_text if raw_text is not None else cleaned

    other_match = _OTHER_STATE_ABBR_RE.search(state_check_text) or _OTHER_STATE_NAME_RE.search(state_check_text)
    if other_match and not _STATE_RE.search(state_check_text):
        other_state = next(g for g in other_match.groups() if g).strip()
        # 2-letter postal codes read as shouting-then-Title-Case if .title()'d
        # ("CA" -> "Ca"); only title-case genuine multi-word/full names.
        other_state_display = other_state.upper() if len(other_state) == 2 else other_state.title()
        result["other_state"] = other_state_display
        result["reason"] = (
            f"This address looks like it's in {other_state_display}, not Texas. "
            f"TxtAnOffer only generates Texas TREC contracts -- we can't produce a "
            f"legally valid contract for a property outside Texas."
        )
        return result

    if not _STATE_RE.search(state_check_text) and not _TX_ZIP_RE.search(state_check_text):
        result["warnings"].append(TX_UNVERIFIED_WARNING)
    if len(cleaned.split()) < 3:
        result["warnings"].append("Address looks short. Please confirm city is included before signing.")

    result["valid"] = True
    return result



# --- stub MLS lookup ---------------------------------------------------
# Replace this with a real MLS API call (e.g. Bridge Interactive, Spark API)
# Real version should geocode address and query MLS for property data
APIFY_API_TOKEN = os.environ.get("APIFY_API_TOKEN", "")


def lookup_mls(address: str) -> dict:
    """Look up property details via Apify Realtor.com actor.
    Falls back to empty dict if unavailable (non-blocking)."""
    if not APIFY_API_TOKEN:
        return {}
    try:
        full_address = f"{address}, TX"
        resp = http_requests.post(
            "https://api.apify.com/v2/acts/kawsar~Realtor-Property-Details-Cheap/run-sync-get-dataset-items",
            params={"token": APIFY_API_TOKEN},
            json={"searchQueries": [full_address]},
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            print(f"[MLS] Apify actor failed: {resp.status_code} {resp.text[:200]}")
            return {}
        results = resp.json()
        if not results:
            print(f"[MLS] No results for: {address}")
            return {}
        prop = results[0]
        if not prop.get("beds") and not prop.get("sqft"):
            print(f"[MLS] Property not found on Realtor.com: {address}")
            return {}
        print(f"[MLS] Found: {prop.get('beds', '?')} bed, {prop.get('baths', '?')} bath, {prop.get('sqft', '?')} sqft")
        return {
            "bed": prop.get("beds") or 0,
            "bath": prop.get("baths") or 0,
            "sqft": prop.get("sqft") or 0,
            "lot_sqft": prop.get("lotSqft") or prop.get("lot_sqft") or 0,
            "year_built": prop.get("yearBuilt") or prop.get("year_built") or 0,
            "listing_price": prop.get("listPrice") or prop.get("list_price") or 0,
            "property_type": prop.get("propertyType") or prop.get("property_type") or "",
            "county": prop.get("county") or "",
            "city": prop.get("city") or "",
            "zip": prop.get("zip") or prop.get("postalCode") or "",
            "apn": "",
        }
    except Exception as e:
        print(f"[MLS] Apify lookup error: {e}")
        return {}


def geocode_state_signal(address: str):
    """Best-effort real-world state check for an address with NO explicit
    state in the text (e.g. bare "Long Beach"), using the same Apify/
    Realtor.com actor as MLS enrichment -- but searched exactly as typed,
    not lookup_mls()'s forced ", TX" suffix, since that would hide the very
    signal we're looking for. Only called for addresses validate_address()
    already flagged as ambiguous (_tx_needs_confirm), not on every message.

    Scans every string/int value in the result for a state signal rather
    than depending on one specific field name -- Apify actors change their
    schema without notice, and a missed field name would silently defeat
    this check. Returns the detected other-state text if the real listing
    data disagrees with Texas, else None (no result, API unavailable, or
    agrees with Texas -- meaning: not blocked)."""
    if not APIFY_API_TOKEN:
        return None
    try:
        resp = http_requests.post(
            "https://api.apify.com/v2/acts/kawsar~Realtor-Property-Details-Cheap/run-sync-get-dataset-items",
            params={"token": APIFY_API_TOKEN},
            json={"searchQueries": [address]},
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            return None
        results = resp.json()
        if not results:
            return None
        prop = results[0]
        # Check each field VALUE independently rather than joining them into
        # one string -- structured fields like {"state": "CA"} won't satisfy
        # the SMS-text regexes above, which expect state codes in address
        # position (", CA" or "CA 90802"), not standing alone.
        str_values = [v.strip() for v in prop.values() if isinstance(v, str) and v.strip()]
        other_abbrs = set(_OTHER_STATE_ABBRS.split("|"))
        if any(_STATE_RE.search(v) for v in str_values):
            return None  # real listing data agrees with Texas
        for v in str_values:
            if v.upper() in other_abbrs:
                return v.upper()
            other = _OTHER_STATE_ABBR_RE.search(v) or _OTHER_STATE_NAME_RE.search(v)
            if other:
                return next(g for g in other.groups() if g)
        return None
    except Exception as e:
        print(f"[GEOCODE] state check failed: {e}")
        return None


def _normalize_addr(s):
    import re as _re
    return _re.sub(r'[^a-z0-9]', '', (s or "").lower())


def find_recent_offer(phone: str, address_query: str):
    """Fuzzy-match an address against an agent's offer history for AMEND lookups.
    Tolerant of abbreviations/partial text since agents won't retype the exact
    stored address -- most recent match wins (get_offers_for_phone is DESC)."""
    nq = _normalize_addr(address_query)
    for o in get_offers_for_phone(phone):
        no = _normalize_addr(o["address"])
        if nq and (nq in no or no in nq):
            return o
    return None


def other_state_block_message(source_id: str, address: str, other_state: str) -> str:
    """SMS copy for a confirmed non-Texas address, plus a tracked event so a
    WAITLIST reply (see the SMS keyword handler) can look up which state
    this phone was actually asking about. source_id is the agent's phone
    for real SMS/-- for the /demo and /api/demo bypasses it's a fixed
    pseudo-id ("demo-web"/"landing-demo"), which is fine: those don't
    support WAITLIST capture since there's no real phone to notify later."""
    track_event("blocked_other_state", source_id, {"state": other_state, "address": address})
    return (
        f"This looks like it's in {other_state}, not Texas. TxtAnOffer "
        f"currently only supports Texas. Want to know when we launch in "
        f"{other_state}? Reply WAITLIST to join the list."
    )


def build_offer_draft(incoming_msg: str, source_id: str):
    """Parse -> validate address -> lookup MLS -> compute money fields, but
    stop short of generating the PDF. Used by the AI Offer Builder
    confirmation flow, which shows this back to the agent and waits for a
    YES before actually calling fill_offer_pdf.
    Returns (parsed, error_or_None, warnings)."""
    parsed = parse_offer_sms(incoming_msg)
    if "error" in parsed:
        return parsed, parsed["error"], []

    addr_check = validate_address(parsed.get("address", ""), raw_text=incoming_msg)
    if not addr_check["valid"]:
        if addr_check.get("other_state"):
            msg = other_state_block_message(source_id, addr_check["normalized"], addr_check["other_state"])
            return parsed, msg, []
        return parsed, addr_check["reason"], []
    parsed["address"] = addr_check["normalized"]
    warnings = addr_check["warnings"]
    parsed["_tx_needs_confirm"] = TX_UNVERIFIED_WARNING in warnings

    # No state was mentioned at all (e.g. bare "Long Beach") -- before
    # falling back to the soft "quick check, reply 1/2" flow, try a real
    # geocode lookup that can actually discover the property is out of
    # state. Only fires for the ambiguous case; an explicit state was
    # already handled (hard block or pass) by validate_address() above.
    if parsed["_tx_needs_confirm"]:
        other_state = geocode_state_signal(parsed["address"])
        if other_state:
            msg = other_state_block_message(source_id, parsed["address"], other_state)
            return parsed, msg, []

    # Get MLS data
    mls_data = lookup_mls(parsed["address"])

    # Use agent-specified county/city if provided, otherwise use MLS lookup
    if "county" not in parsed:
        parsed["county"] = mls_data.get("county", "")
    if "city" not in parsed:
        parsed["city"] = mls_data.get("city", "")

    # Add other MLS data (bed/bath/sqft)
    parsed.update({k: v for k, v in mls_data.items() if k not in ["county", "city"]})

    # Get agent profile
    agent = get_agent_profile(source_id)
    parsed["agent"] = agent

    # Smart calculations
    price = parsed["price"]
    down_pct = parsed["down_payment_pct"]

    parsed["down_payment_amount"] = int(price * down_pct)
    parsed["loan_amount"] = price - parsed["down_payment_amount"]
    parsed["earnest_money"] = int(price * agent["default_earnest_pct"])
    parsed["option_fee"] = agent["default_option_fee"]

    return parsed, None, warnings


def process_offer(incoming_msg: str, source_id: str):
    """Shared logic: build_offer_draft() then immediately fill the PDF, no
    confirmation step. Used by callers that don't do the AI Offer Builder
    conversation (e.g. the homepage's instant /api/demo widget).
    Returns (parsed, pdf_path_or_None, error_or_None, warnings)."""
    parsed, error, warnings = build_offer_draft(incoming_msg, source_id)
    if error:
        return parsed, None, error, warnings

    # This path has no confirmation step at all -- it must never generate a
    # PDF for an address we couldn't confirm is in Texas (validate_address()
    # already hard-blocks an explicit other-state signal; this catches the
    # remaining "no state mentioned, geocode inconclusive" case instead of
    # silently proceeding).
    if parsed.get("_tx_needs_confirm"):
        return parsed, None, TX_NEEDS_STATE_MESSAGE, warnings

    try:
        pdf_path = fill_offer_pdf(parsed, source_id)
    except Exception as e:
        return parsed, None, f"Parsed OK but couldn't generate the PDF yet: {e}", warnings

    return parsed, pdf_path, None, warnings


FINANCING_LABELS = {"conventional": "Conventional", "fha": "FHA", "va": "VA", "cash": "Cash"}


def _fmt_pct(pct: float) -> str:
    pct100 = pct * 100
    return f"{pct100:.0f}" if pct100 == int(pct100) else f"{pct100:.1f}"


def format_offer_confirmation(parsed: dict) -> str:
    """The AI Offer Builder's structured confirmation, shown before a PDF is
    generated. Financing/inspection lines only appear when the agent actually
    specified them -- never silently guessed. Agent name/license line is
    omitted if the agent hasn't set up their profile yet."""
    close_dt = datetime.now() + timedelta(days=parsed["close_days"])

    addr_parts = [parsed["address"]]
    if parsed.get("city"):
        addr_parts.append(parsed["city"])
    if parsed.get("zip"):
        addr_parts.append(f"TX {parsed['zip']}")
    elif parsed.get("city"):
        addr_parts.append("TX")
    full_addr = ", ".join(addr_parts)

    lines = [
        "Got it.",
        "",
        full_addr,
        f"Price: ${parsed['price']:,} | Down: {_fmt_pct(parsed['down_payment_pct'])}% (${parsed['down_payment_amount']:,})",
        f"Loan: ${parsed['loan_amount']:,} | Earnest: ${parsed['earnest_money']:,} | Option: ${parsed['option_fee']:,}",
        f"Close: {close_dt.strftime('%b %d')} ({parsed['close_days']} days)",
    ]
    if parsed.get("financing_type"):
        lines.append(f"Financing: {FINANCING_LABELS.get(parsed['financing_type'], parsed['financing_type'].title())}")
    if parsed.get("inspection_days") is not None:
        lines.append(f"Inspection: {parsed['inspection_days']} days")

    lines += [
        "",
        "Reply YES to generate TREC draft.",
        'Reply with corrections (ex: "make it 820k").',
    ]

    agent = parsed.get("agent") or {}
    if agent.get("name"):
        agent_line = agent["name"]
        if agent.get("license"):
            agent_line += f" - Lic #{agent['license']}"
        lines += ["", agent_line]

    lines.append("Msg rates may apply. Reply STOP to unsubscribe, HELP for help.")
    return "\n".join(lines)


TX_NEEDS_STATE_MESSAGE = (
    "Can't confirm this address is in Texas -- TxtAnOffer only generates "
    "Texas TREC contracts. Resend your offer with the city and state "
    'included, e.g. "725k 3% 21day 123 Main St, Austin, TX".'
)


def format_tx_confirmation(parsed: dict) -> str:
    """Shown instead of the normal confirmation when validate_address()
    couldn't confirm the property is in Texas (and a real geocode lookup
    either wasn't available or didn't resolve it either). Deliberately asks
    for a full resend rather than a one-keystroke "reply 1 for yes, Texas"
    choice -- a single lazy tap on a wrong-state address is exactly how this
    generated a California TREC contract before; see git history around
    "geocode_state_signal" / TX_NEEDS_STATE_MESSAGE for the incident."""
    return (
        f"Got it for {parsed['address']}.\n\n"
        f"{TX_NEEDS_STATE_MESSAGE}\n\n"
        f"Reply STOP to unsubscribe, HELP for help."
    )


def twilio_send_sms(to, body):
    """Send an SMS via the Twilio REST API (for out-of-band sends, e.g. login/signup links)."""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        print("[SMS] TWILIO_ACCOUNT_SID/AUTH_TOKEN not set, skipping send")
        return False
    try:
        client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(to=to, from_=TWILIO_PHONE_NUMBER, body=body)
    except Exception as e:
        print(f"[SMS] Twilio send failed: {e}")
        # Surfaced on /analytics under "Recent Send Failures" -- without this,
        # a blocked send (e.g. A2P 10DLC error 30034) looks identical to a
        # working reply from the agent's side and only shows up in Railway logs.
        track_event("sms_send_failed", to, {"error": str(e), "body": body[:80]})
        return False
    print(f"[SMS] Twilio sent to {to}: {body[:50]}...")
    return True


def finalize_offer_sms(agent_phone: str, draft: dict):
    """Shared finalize step for the YES confirmation: checks the offer
    limit, generates the PDF, records it, and texts back the result.
    Always sends exactly one SMS.

    Single choke point for every path that can generate a PDF from SMS, so
    it's also where the Texas-state guard lives: a draft that still needs
    state confirmation must never reach fill_offer_pdf, regardless of which
    keyword (YES/CREATE/CONFIRM) got it here."""
    if draft.get("_tx_needs_confirm"):
        twilio_send_sms(agent_phone, TX_NEEDS_STATE_MESSAGE)
        return
    try:
        can_generate, reason, user = can_generate_offer(agent_phone)
        if not can_generate:
            track_event("limit_reached", agent_phone)
            payment_url = request.host_url.rstrip("/") + "/pricing"
            twilio_send_sms(agent_phone,
                f"You've used your {FREE_OFFER_LIMIT} free offers!\n"
                f"Subscribe for unlimited: {payment_url}\n"
                f"$39/mo, cancel anytime"
            )
            return

        pdf_path = fill_offer_pdf(draft, agent_phone)
    except Exception as e:
        print(f"[SMS] Finalize ERROR: {e}")
        import traceback
        traceback.print_exc()
        twilio_send_sms(agent_phone, "Error generating offer. Please try again or contact support.")
        return

    clear_draft(agent_phone)
    track_event("offer_generated", agent_phone, {"price": draft.get("price")})
    new_count = increment_offer_count(agent_phone)
    if new_count == FREE_OFFER_LIMIT and reason == "free_trial":
        track_event("trial_completed", agent_phone)

    filename = os.path.basename(pdf_path)
    pdf_url = sign_pdf_url(filename, request.host_url.rstrip("/"))
    record_offer(agent_phone, draft, filename)
    fire_webhook(agent_phone, draft, pdf_url)

    if reason in ("subscribed", "admin"):
        status_line = ""
    else:
        remaining = FREE_OFFER_LIMIT - new_count
        if remaining > 0:
            status_line = f"\n{remaining} free offers remaining"
        else:
            payment_url = request.host_url.rstrip("/") + "/pricing"
            status_line = f"\nLast free offer! Subscribe for unlimited:\n{payment_url}"

    includes = "TREC 20-19"
    if draft.get("loan_amount", 0) > 0:
        includes += " + 40-11 Financing Addendum"
    includes += " + 61-0 Water Disclosure"
    if draft.get("has_hoa"):
        includes += " + HOA Addendum"
    includes += " + IABS"

    # One-time nudge on the very first offer only -- an agent with no saved
    # profile sees a much longer blocking-fields checklist on the review
    # page (Title Company, Escrow Agent, Buyer's-agent contact all show up
    # missing) than one who's filled it in once. Told here, once, instead of
    # leaving them to discover the checklist shrinks only by trial and error.
    profile_nudge = ""
    if new_count == 1:
        agent_profile = draft.get("agent") or {}
        if not agent_profile.get("title_company") or not agent_profile.get("business_address"):
            profile_url = request.host_url.rstrip("/") + "/profile"
            profile_nudge = (
                f"\n\nTip: add your Title Company & Business Address once at "
                f"{profile_url} and future offers will need far less filled in by hand."
            )

    reply = (
        f"DONE. TREC draft for {draft['address']} ready:\n\n"
        f"Review & Email: {pdf_url}\n\n"
        f"Includes: {includes}. DRAFT - Agent must review before signing.\n\n"
        f"Need to change price/terms? Just text new offer.\n\n"
        f"Reply DASHBOARD for all offers. STOP to unsubscribe, HELP for help."
        f"{status_line}"
        f"{profile_nudge}"
    )
    twilio_send_sms(agent_phone, reply)


SMS_HELP_TEXT = (
    "TxtAnOffer Commands:\n\n"
    "Text your offer terms anytime -- TxtAnOffer turns them into a "
    "signed-ready PDF in seconds.\n\n"
    "HELP or MENU - This menu\n"
    "DASHBOARD - Your offer history\n"
    "STATUS - Plan & usage\n"
    "PROFILE - Edit agent info\n\n"
    "To generate an offer, text:\n"
    "price down% days address\n"
    "(optionally add financing type and inspection days)\n\n"
    "Examples:\n"
    "725k 3% 21day 123 Main St\n"
    "725k 10% down conventional close Sept 15 10-day inspection 123 Main St\n\n"
    "You'll get a confirmation to review -- reply YES to create the PDF, "
    "NO to cancel, or send corrections.\n\n"
    "To amend an existing offer:\n"
    "AMEND <address> price <value>\n"
    "AMEND <address> close +<days>\n\n"
    "Examples:\n"
    "AMEND 123 Main St price 730k\n"
    "AMEND 123 Main St close +10\n\n"
    "Reply STOP to unsubscribe."
)


_HELP_SYNONYMS = ("HELP", "MENU", "COMMAND", "COMMANDS", "CMD", "OPTIONS", "INFO")


def _is_help_keyword(word: str) -> bool:
    """Exact match against HELP/MENU and reasonable synonyms ("command",
    "cmd"), plus typo tolerance ("hlp", "menuu") on HELP/MENU specifically,
    so a mistyped or differently-worded text still reaches the command list
    instead of silently falling through to the offer parser."""
    if word in _HELP_SYNONYMS:
        return True
    if not word.isalpha() or not (2 <= len(word) <= 6):
        return False
    return bool(difflib.get_close_matches(word, ("HELP", "MENU"), n=1, cutoff=0.75))


@app.route("/sms", methods=["GET", "POST"])
def sms_reply():
    if request.method == "GET":
        return redirect("/")

    # Twilio posts inbound SMS as form-encoded params (Body, From), validated by signature
    result = parse_incoming_sms()
    if not isinstance(result, tuple) or len(result) != 3:
        # parse_incoming_sms returns a Flask response on failure (e.g., 403 signature error)
        return result
    _form, incoming_msg, agent_phone = result

    # Log all incoming SMS for debugging
    print(f"[SMS] From: {agent_phone}, Body: {incoming_msg}")
    track_event("sms_received", agent_phone, {"body": incoming_msg})
    run_cleanup_if_due(OUTPUT_DIR)
    run_reminders_if_due(twilio_send_sms)
    run_tc_followup_if_due()

    # Handle keywords
    keyword = incoming_msg.strip().upper()

    if _is_help_keyword(keyword):
        twilio_send_sms(agent_phone, SMS_HELP_TEXT)
        return "", 200

    if keyword == "WAITLIST":
        state = get_last_blocked_state(agent_phone)
        track_event("waitlist_joined", agent_phone, {"state": state})
        if state:
            twilio_send_sms(agent_phone,
                f"You're on the list! We'll text you the moment TxtAnOffer "
                f"supports {state}. Reply STOP to unsubscribe.")
        else:
            twilio_send_sms(agent_phone,
                "You're on the list! We'll text you when TxtAnOffer expands "
                "outside Texas. Reply STOP to unsubscribe.")
        return "", 200

    if keyword.startswith("AMEND "):
        amend = parse_amendment_sms(incoming_msg)
        if "error" in amend:
            twilio_send_sms(agent_phone, amend["error"])
            return "", 200
        offer = find_recent_offer(agent_phone, amend["address"])
        if not offer:
            twilio_send_sms(agent_phone,
                f'No offer found matching "{amend["address"]}". '
                f'Text DASHBOARD to see your offer history.')
            return "", 200
        try:
            pdf_path = fill_amendment_pdf(offer, amend)
        except Exception as e:
            print(f"[SMS] Amendment ERROR: {e}")
            twilio_send_sms(agent_phone, "Error generating amendment. Please try again or contact support.")
            return "", 200
        filename = os.path.basename(pdf_path)
        record_amendment(offer["id"], agent_phone, amend["field"], amend["value"], filename)
        pdf_url = sign_pdf_url(filename, request.host_url.rstrip("/"))
        if amend["field"] == "price":
            change_line = f"New Sales Price: ${amend['value']:,}"
        else:
            change_line = f"Closing extended {amend['value']} days"
        twilio_send_sms(agent_phone,
            f"Amendment (TREC 39-11) for {offer['address']}:\n{change_line}\n\n{pdf_url}\n\n"
            f"Draft only -- review before signing.\n\n"
            f"Reply STOP to unsubscribe, HELP for help.")
        return "", 200

    if keyword == "DASHBOARD":
        dash_link = sign_dashboard_url(agent_phone, request.host_url.rstrip("/"))
        twilio_send_sms(agent_phone, f"Your dashboard:\n{dash_link}")
        return "", 200

    if keyword == "STATUS":
        user = get_user(agent_phone)
        if not user:
            create_user(agent_phone)
            twilio_send_sms(agent_phone, f"Welcome! You have {FREE_OFFER_LIMIT} free offers.\n\nJust text your offer:\n725k 3% 21day 123 Main St\n\nReply HELP for all commands.")
            return "", 200
        elif is_admin_phone(agent_phone):
            twilio_send_sms(agent_phone, f"Plan: Admin (Unlimited)\nOffers generated: {user['offer_count']}\n\nText HELP for commands.")
        elif user["is_subscribed"]:
            twilio_send_sms(agent_phone, f"Plan: Unlimited\nOffers generated: {user['offer_count']}\n\nText HELP for commands.")
        else:
            remaining = max(0, FREE_OFFER_LIMIT - user["offer_count"])
            twilio_send_sms(agent_phone, f"Plan: Free trial\nOffers used: {user['offer_count']}/{FREE_OFFER_LIMIT}\nRemaining: {remaining}\n\nUpgrade: txtanoffer.com/pricing")
        return "", 200

    if keyword == "PROFILE":
        profile_link = sign_dashboard_url(agent_phone, request.host_url.rstrip("/")).replace("/dashboard?", "/profile?")
        twilio_send_sms(agent_phone, f"Edit your agent profile:\n{profile_link}\n\nYour name, license, brokerage, and defaults auto-fill into every contract.")
        return "", 200

    if keyword in ("YES", "Y", "CONFIRM", "CREATE"):
        draft = get_draft(agent_phone)
        if not draft:
            twilio_send_sms(agent_phone, "No pending offer to confirm. Text your offer details to get started.")
            return "", 200
        finalize_offer_sms(agent_phone, draft)
        return "", 200

    if keyword in ("NO", "CANCEL"):
        if get_draft(agent_phone):
            clear_draft(agent_phone)
            twilio_send_sms(agent_phone, "Offer cancelled. Text new details anytime.")
        else:
            twilio_send_sms(agent_phone, "Nothing pending to cancel.")
        return "", 200

    try:
        # Check subscription status
        can_generate, reason, user = can_generate_offer(agent_phone)
        print(f"[SMS] Subscription check: can_generate={can_generate}, reason={reason}")

        if not can_generate:
            track_event("limit_reached", agent_phone)
            payment_url = request.host_url.rstrip("/") + "/pricing"
            twilio_send_sms(agent_phone,
                f"You've used your {FREE_OFFER_LIMIT} free offers!\n"
                f"Subscribe for unlimited: {payment_url}\n"
                f"$39/mo, cancel anytime"
            )
            return "", 200

        # Build the draft (parse -> validate -> MLS -> money math), but don't
        # generate the PDF yet -- show a confirmation first and wait for YES.
        parsed, error, warnings = build_offer_draft(incoming_msg, agent_phone)

        if error:
            # A short reply like "make it 820k" won't parse as a full new
            # offer -- if there's already a pending draft, try merging it in
            # as a correction instead of treating this as a failed first
            # attempt.
            existing_draft = get_draft(agent_phone)
            if existing_draft:
                correction = parse_correction_sms(incoming_msg)
                if correction:
                    existing_draft.update(correction)
                    if "price" in correction or "down_payment_pct" in correction:
                        agent = existing_draft.get("agent") or {}
                        existing_draft["down_payment_amount"] = int(existing_draft["price"] * existing_draft["down_payment_pct"])
                        existing_draft["loan_amount"] = existing_draft["price"] - existing_draft["down_payment_amount"]
                        existing_draft["earnest_money"] = int(existing_draft["price"] * agent.get("default_earnest_pct", 0.01))
                    save_draft(agent_phone, existing_draft)
                    twilio_send_sms(agent_phone, format_offer_confirmation(existing_draft))
                    return "", 200
                twilio_send_sms(agent_phone,
                    'Didn\'t catch a change. Try something like "make it 820k" or '
                    '"close in 25 days", or reply YES to confirm as-is, NO to cancel.'
                )
                return "", 200

            partial = parse_offer_sms(incoming_msg)
            hints = []
            if partial.get("price"):
                hints.append(f"${partial['price']:,}")
            if partial.get("down_payment_pct"):
                hints.append(f"{partial['down_payment_pct']*100:.0f}%")
            if partial.get("close_days"):
                hints.append(f"{partial['close_days']}day")
            if partial.get("address"):
                hints.append(partial["address"])
            # Only show the "Got X / Need Y" nudge when something's still
            # genuinely missing. Once price/down%/days/address all parsed,
            # any error past that point (address rejected, blocked for
            # another state) already explains itself in `error` -- tacking
            # "Need: price, down%, days, address" underneath a message that
            # already has all four is just confusing.
            have_all_core_fields = all(partial.get(k) for k in ("price", "down_payment_pct", "close_days", "address"))
            hint_line = ""
            if hints and not have_all_core_fields:
                hint_line = f"\n\nGot: {' . '.join(hints)}\nNeed: price, down%, days, address"
            twilio_send_sms(agent_phone, f"{error}{hint_line}")
            return "", 200

        save_draft(agent_phone, parsed)
        if TX_UNVERIFIED_WARNING in warnings:
            twilio_send_sms(agent_phone, format_tx_confirmation(parsed))
            return "", 200
        warning_line = f"\n\nNote: {' / '.join(warnings)}" if warnings else ""
        twilio_send_sms(agent_phone, format_offer_confirmation(parsed) + warning_line)
        return "", 200

    except Exception as e:
        print(f"[SMS] ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        twilio_send_sms(agent_phone, "Error generating offer. Please try again or contact support.")
        return "", 200


DEMO_FORM = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Demo — TxtAnOffer</title>
<meta name="description" content="Generate TREC purchase offers in 10 seconds via text or web. Texas real estate agents save 45 minutes per offer.">
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
  :root {{
    --bg: #F5F5F7;
    --bg-card: #fff;
    --border: rgba(15,31,47,0.08);
    --border-hover: rgba(0,0,0,0.35);
    --text: #0f1f2f;
    --text-muted: #5a6b7a;
    --text-dim: #8a9aa9;
    --accent: #171717;
    --accent-light: #525252;
    --accent-dark: #000000;
    --accent-tint: #F0F0EE;
    --radius: 1.25rem;
    --radius-sm: 0.85rem;
    --transition: all 0.2s ease;
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  html {{ scroll-behavior:smooth; }}
  body {{
    font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
    background:var(--bg);
    color:var(--text);
    line-height:1.5;
    -webkit-font-smoothing:antialiased;
    min-height:100vh;
  }}
  a {{ color:inherit; text-decoration:none; }}

  /* Nav */
  .nav {{
    display:flex;align-items:center;justify-content:space-between;
    padding:1rem 2rem;position:sticky;top:0;
    background:rgba(255,255,255,0.85);backdrop-filter:blur(20px);
    -webkit-backdrop-filter:blur(20px);
    border-bottom:1px solid var(--border);z-index:100;
  }}
  .nav-left {{display:flex;align-items:center;gap:0.6rem;font-weight:700;font-size:1.1rem;letter-spacing:-0.02em;color:var(--text);}}
  .nav-logo {{width:34px;height:34px;border-radius:22%;overflow:hidden;}}
  .nav-logo img {{width:100%;height:100%;object-fit:contain;}}
  .nav-links {{display:flex;gap:2rem;font-size:0.875rem;font-weight:500;color:var(--text-muted);}}
  .nav-links a {{transition:var(--transition);}}
  .nav-links a:hover {{color:var(--text);}}
  .nav-cta {{
    background:var(--accent);color:#fff;padding:0.55rem 1.35rem;border-radius:9999px;
    font-size:0.875rem;font-weight:600;text-decoration:none;display:inline-block;
    transition:var(--transition);
  }}
  .nav-cta:hover {{transform:scale(1.05);box-shadow:0 0 24px rgba(0,0,0,0.25);}}
  .nav-toggle {{ display: none; flex-direction: column; justify-content: center; gap: 5px; width: 34px; height: 34px; background: none; border: none; cursor: pointer; padding: 0; }}
  .nav-toggle span {{ display: block; width: 100%; height: 2px; background: var(--text); border-radius: 2px; }}

  /* Page layout */
  .page {{max-width:580px;margin:0 auto;padding:4rem 1.5rem;overflow-x:hidden;width:100%;}}
  .page-badge {{
    display:inline-flex;align-items:center;gap:0.4rem;
    background:var(--accent-tint);border:1px solid rgba(0,0,0,0.12);
    color:var(--accent-dark);font-size:0.7rem;font-weight:700;
    padding:0.35rem 0.85rem;border-radius:9999px;
    text-transform:uppercase;letter-spacing:0.06em;margin-bottom:1rem;
  }}
  .page h1 {{font-size:2.25rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.5rem;color:var(--text);}}
  .page h1 .gradient {{
    background:linear-gradient(135deg,var(--accent-light),var(--accent));
    -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
  }}
  .page-sub {{color:var(--text-muted);font-size:1rem;line-height:1.6;margin-bottom:2rem;}}

  /* Workflow */
  .workflow {{
    display:flex;align-items:center;justify-content:center;gap:0.5rem;
    margin-bottom:2rem;padding:1.25rem;
    background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
    box-shadow:0 1px 3px rgba(15,31,47,0.05);
  }}
  .wf-step {{text-align:center;flex:1;}}
  .wf-icon {{font-size:1.5rem;margin-bottom:0.4rem;}}
  .wf-title {{font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:var(--text);}}
  .wf-desc {{font-size:0.7rem;color:var(--text-dim);margin-top:0.2rem;line-height:1.4;}}
  .wf-arrow {{color:var(--accent);font-size:1.25rem;opacity:0.7;}}

  /* Card */
  .card {{
    background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
    padding:2rem;overflow:hidden;max-width:100%;box-shadow:0 1px 3px rgba(15,31,47,0.05);
  }}
  .field-label {{
    font-size:0.7rem;font-weight:700;color:var(--text-dim);
    text-transform:uppercase;letter-spacing:0.07em;margin-bottom:0.5rem;display:block;
  }}
  .card input[type=text] {{
    width:100%;background:#fff;border:1px solid rgba(15,31,47,0.14);
    border-radius:var(--radius-sm);padding:0.8rem 1rem;color:var(--text);
    font-size:0.95rem;font-family:inherit;outline:none;transition:var(--transition);
  }}
  .card input[type=text]:focus {{border-color:var(--accent);box-shadow:0 0 0 3px rgba(0,0,0,0.1);}}
  .card input[type=text]::placeholder {{color:#b8c2ca;}}
  .card button {{
    width:100%;margin-top:0.75rem;
    background:linear-gradient(135deg,var(--accent),#000);color:#fff;border:none;
    border-radius:var(--radius-sm);padding:0.85rem;font-weight:600;font-size:0.95rem;
    font-family:inherit;cursor:pointer;transition:var(--transition);
  }}
  .card button:hover {{transform:translateY(-2px);box-shadow:0 8px 24px rgba(0,0,0,0.25);}}
  .hint {{font-size:0.75rem;color:var(--text-dim);margin-top:0.5rem;}}

  /* Result */
  .result {{margin-top:1.5rem;padding-top:1.5rem;border-top:1px solid var(--border);}}
  .result-stamp {{
    display:inline-flex;align-items:center;gap:0.4rem;
    font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;
    color:var(--accent-dark);background:var(--accent-tint);border:1px solid rgba(0,0,0,0.12);
    padding:0.3rem 0.7rem;border-radius:9999px;margin-bottom:1rem;
  }}
  .result-addr {{font-size:1.25rem;font-weight:700;color:var(--text);margin-bottom:1rem;}}
  .result-row {{display:flex;justify-content:space-between;padding:0.5rem 0;font-size:0.9rem;border-bottom:1px solid var(--border);}}
  .result-row .k {{color:var(--text-dim);font-size:0.8rem;text-transform:uppercase;letter-spacing:0.04em;font-weight:600;}}
  .result-row .v {{color:var(--text);font-weight:500;}}
  .result-ready {{font-size:0.85rem;color:var(--accent-dark);margin-top:1rem;}}

  .pdf-preview {{margin-top:1.25rem;border:1px solid var(--border);border-radius:var(--radius-sm);overflow:hidden;max-width:100%;}}
  .pdf-preview-label {{
    font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;
    color:var(--text-dim);padding:0.6rem 1rem;background:rgba(15,31,47,0.02);
    border-bottom:1px solid var(--border);
  }}
  .pdf-frame {{width:100%;height:560px;border:none;background:#f1f5f9;}}
  .pdf-mobile {{display:none;padding:1.5rem;text-align:center;background:rgba(15,31,47,0.02);}}
  .pdf-mobile a {{color:var(--accent-dark);font-weight:600;font-size:0.9rem;text-decoration:none;}}
  .pdf-mobile a:hover {{text-decoration:underline;}}
  @media(max-width:768px){{
    .pdf-frame {{display:none;}}
    .pdf-mobile {{display:block;}}
  }}

  .download-btn {{
    margin-top:1rem;display:block;text-align:center;
    background:linear-gradient(135deg,var(--accent),#000);color:#fff;
    font-weight:600;font-size:0.9rem;padding:0.85rem;border-radius:var(--radius-sm);
    text-decoration:none;transition:var(--transition);
  }}
  .download-btn:hover {{transform:translateY(-2px);box-shadow:0 8px 24px rgba(0,0,0,0.25);}}
  .disclaimer {{margin-top:1rem;font-size:0.75rem;color:var(--text-dim);line-height:1.5;font-style:italic;}}

  /* Integration buttons */
  .integration-actions {{display:flex;gap:0.5rem;margin:1.25rem 0 0;flex-wrap:wrap;}}
  .int-btn {{
    flex:1;min-width:110px;padding:0.6rem 0.75rem;font-size:0.75rem;font-weight:600;
    border:1px solid var(--border);background:var(--bg-card);color:var(--text-muted);
    border-radius:var(--radius-sm);cursor:pointer;font-family:inherit;
    letter-spacing:0.02em;transition:var(--transition);
  }}
  .int-btn:hover {{border-color:var(--accent);color:var(--accent-dark);}}

  /* Modals */
  .modal {{position:fixed;inset:0;background:rgba(15,31,47,0.5);display:flex;align-items:center;
    justify-content:center;z-index:1000;padding:20px;}}
  .modal-box {{
    background:var(--bg-card);padding:2rem;border-radius:var(--radius);width:100%;max-width:380px;
    position:relative;border:1px solid var(--border);box-shadow:0 20px 60px rgba(15,31,47,0.2);
  }}
  .modal-title {{font-size:1.1rem;font-weight:700;color:var(--text);margin:0 0 1rem;}}
  .modal-desc {{font-size:0.85rem;color:var(--text-muted);margin:0 0 0.75rem;line-height:1.5;}}
  .modal-input {{
    width:100%;font-family:inherit;font-size:0.9rem;padding:0.7rem 0.85rem;
    border:1px solid rgba(15,31,47,0.14);background:#fff;color:var(--text);
    border-radius:var(--radius-sm);outline:none;margin-bottom:0.6rem;
  }}
  .modal-input:focus {{border-color:var(--accent);}}
  .modal-submit {{
    width:100%;padding:0.75rem;background:var(--accent);color:#fff;border:none;
    font-family:inherit;font-size:0.9rem;font-weight:600;border-radius:var(--radius-sm);cursor:pointer;
  }}
  .modal-submit:hover {{background:#000;}}
  .modal-box .modal-close {{
    position:absolute;top:0.75rem;right:1rem;width:auto;margin-top:0;
    background:none;border:none;border-radius:0;padding:0;
    font-size:1.5rem;font-weight:400;line-height:1;color:var(--text-dim);cursor:pointer;
  }}
  .modal-box .modal-close:hover {{transform:none;box-shadow:none;color:var(--text);}}
  .modal-status {{margin-top:0.6rem;font-size:0.8rem;color:var(--text-dim);}}
  .modal-status.success {{color:var(--accent-dark);}}
  .modal-status.fail {{color:#dc2626;}}

  /* Share */
  .share-section {{margin-top:1.25rem;padding-top:1.25rem;border-top:1px solid var(--border);}}
  .share-label {{font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;
    color:var(--text-dim);margin-bottom:0.6rem;display:block;text-align:center;}}
  .share-buttons {{display:flex;gap:0.5rem;justify-content:center;}}
  .share-btn {{
    flex:1;max-width:130px;padding:0.6rem 0.75rem;text-align:center;text-decoration:none;
    border-radius:var(--radius-sm);font-size:0.8rem;font-weight:600;transition:opacity 0.2s;
    display:flex;align-items:center;justify-content:center;gap:0.4rem;
  }}
  .share-btn:hover {{opacity:0.85;}}
  .share-twitter {{background:#1DA1F2;color:white;}}
  .share-linkedin {{background:#0A66C2;color:white;}}
  .share-copy {{background:var(--bg-card);color:var(--text-muted);cursor:pointer;border:1px solid var(--border);}}
  .share-copy.copied {{background:var(--accent);border-color:var(--accent);color:#fff;}}

  /* Warning / Error */
  .error {{
    margin-top:1.25rem;padding:1rem;background:rgba(239,68,68,0.08);
    border:1px solid rgba(239,68,68,0.2);border-radius:var(--radius-sm);
    font-size:0.85rem;color:#dc2626;
  }}
  .warning-note {{
    margin:1rem 0 0.75rem;padding:0.85rem 1rem;background:rgba(245,158,11,0.1);
    border:1px solid rgba(245,158,11,0.25);border-radius:var(--radius-sm);
    font-size:0.8rem;color:#b45309;line-height:1.5;
  }}
  .warning-note .wn-title {{font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.04em;margin-bottom:0.25rem;}}

  /* SMS command menu */
  .cmd-menu {{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:1.5rem;margin-top:1.5rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);}}
  .cmd-menu-title {{font-size:0.7rem;font-weight:700;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.06em;margin-bottom:1rem;}}
  .cmd-row {{display:flex;gap:1rem;padding:0.55rem 0;border-bottom:1px solid var(--border);align-items:baseline;}}
  .cmd-row:last-child {{border-bottom:none;}}
  .cmd-key {{font-family:monospace;color:var(--accent-dark);font-size:0.85rem;flex:0 0 auto;white-space:nowrap;}}
  .cmd-desc {{color:var(--text-dim);font-size:0.85rem;}}
  @media(max-width:600px){{.cmd-row {{flex-direction:column;gap:0.15rem;}}}}

  /* Trust */
  .trust {{display:flex;gap:1.5rem;margin-top:2rem;justify-content:center;}}
  .trust-item {{text-align:center;}}
  .trust-val {{font-size:1.25rem;font-weight:800;color:var(--accent-dark);}}
  .trust-label {{font-size:0.7rem;color:var(--text-dim);margin-top:0.2rem;font-weight:500;text-transform:uppercase;letter-spacing:0.04em;}}

  /* Footer */
  .foot {{text-align:center;margin-top:2rem;font-size:0.8rem;color:var(--text-dim);line-height:1.6;}}
  .foot a {{color:var(--accent-dark);text-decoration:none;}}
  .foot a:hover {{text-decoration:underline;}}

  @media(max-width:600px){{
    .page {{padding:2rem 1rem;}}
    .page h1 {{font-size:1.75rem;}}
    .workflow {{flex-direction:column;gap:1rem;}}
    .wf-arrow {{transform:rotate(90deg);}}
    .nav-toggle {{display:flex;}}
    .nav-links {{display:none;position:absolute;top:100%;left:0;right:0;flex-direction:column;gap:0;padding:0.5rem 1.25rem 1.25rem;background:#fff;border-bottom:1px solid rgba(15,31,47,0.08);}}
    .nav-links.open {{display:flex;}}
    .nav-links a {{padding:0.75rem 0;border-bottom:1px solid rgba(15,31,47,0.08);}}
    .nav-links a:last-child {{border-bottom:none;}}
    .card {{padding:1.25rem;}}
    .integration-actions {{flex-direction:column;}}
    .int-btn {{min-width:unset;}}
    .result-row {{font-size:0.8rem;}}
    .download-btn {{font-size:0.85rem;padding:0.75rem;}}
  }}
</style>
</head>
<body>
  <nav class="nav">
    <a href="/" class="nav-left">
      <div class="nav-logo"><img src="/static/logo.svg" alt="TxtAnOffer"></div>
      <span>TxtAnOffer</span>
    </a>
    <div class="nav-links" id="navLinks">
      <a href="/#how">How it works</a>
      <a href="/#trust">Accuracy</a>
      <a href="/pricing">Pricing</a>
      <a href="/demo">Demo</a>
      <a href="/playground">Parser Playground</a>
      <a href="/faq">FAQ</a>
      <a href="/about">About</a>
      <a href="/contact">Support</a>
      <a href="/login">Log In</a>
    </div>
    <a href="/signup" class="nav-cta">Start Free Trial</a>
    <button class="nav-toggle" id="navToggle" aria-label="Menu" aria-expanded="false"><span></span><span></span><span></span></button>
  </nav>
  <script>
  (function(){{
    var t=document.getElementById('navToggle'), l=document.getElementById('navLinks');
    if(!t||!l) return;
    t.addEventListener('click', function(){{
      var open = l.classList.toggle('open');
      t.setAttribute('aria-expanded', open ? 'true' : 'false');
    }});
    l.querySelectorAll('a').forEach(function(a){{
      a.addEventListener('click', function(){{ l.classList.remove('open'); t.setAttribute('aria-expanded','false'); }});
    }});
  }})();
  </script>

  <div class="page">
    <div class="page-badge">Live Demo</div>
    <h1>Get a purchase offer<br><span class="gradient">in 10 seconds.</span></h1>
    <p class="page-sub">Agents spend up to 45 minutes preparing purchase offers. TxtAnOffer reduces that to under 10 seconds.</p>

    <div class="workflow">
      <div class="wf-step"><div class="wf-icon">&#9993;</div><div class="wf-title">You type</div><div class="wf-desc">725k 3% 21day<br>1234 Main St</div></div>
      <div class="wf-arrow">&rarr;</div>
      <div class="wf-step"><div class="wf-icon">&#9881;</div><div class="wf-title">We parse</div><div class="wf-desc">Price, terms &amp;<br>address extracted</div></div>
      <div class="wf-arrow">&rarr;</div>
      <div class="wf-step"><div class="wf-icon">&#9998;</div><div class="wf-title">Contract ready</div><div class="wf-desc">TREC 20-19 PDF<br>filled &amp; downloadable</div></div>
    </div>

    <div class="card">
      <form method="POST" action="/demo">
        <label class="field-label">Offer details</label>
        <input type="text" name="offer_text" placeholder="725k 3% 21day Harris 1234 Westheimer Rd" value="{prefill}">
        <button type="submit">Generate My Contract</button>
        <div class="hint">price &middot; down % &middot; closing days &middot; county (optional) &middot; address &middot; financing type &amp; inspection days (optional)</div>
        <div class="hint">You'll get a confirmation to review first &mdash; reply <code>YES</code> to generate the PDF, <code>NO</code> to cancel, or send corrections.</div>
        <div class="hint">Already sent one? Amend it: <code>AMEND 1234 Westheimer Rd price 730k</code> or <code>AMEND 1234 Westheimer Rd close +10</code></div>
      </form>
      {result_html}
    </div>

    <div class="cmd-menu">
      <div class="cmd-menu-title">Text these to 1-833-897-0333</div>
      <div class="cmd-row"><span class="cmd-key">price down% days address</span><span class="cmd-desc">Get a confirmation to review &mdash; e.g. 725k 3% 21day 123 Main St</span></div>
      <div class="cmd-row"><span class="cmd-key">YES</span><span class="cmd-desc">Confirm the pending offer and generate the PDF</span></div>
      <div class="cmd-row"><span class="cmd-key">NO</span><span class="cmd-desc">Cancel the pending offer</span></div>
      <div class="cmd-row"><span class="cmd-key">AMEND &lt;address&gt; price &lt;value&gt;</span><span class="cmd-desc">Change the sales price on an offer you sent</span></div>
      <div class="cmd-row"><span class="cmd-key">AMEND &lt;address&gt; close +&lt;days&gt;</span><span class="cmd-desc">Push back the closing date on an offer you sent</span></div>
      <div class="cmd-row"><span class="cmd-key">DASHBOARD</span><span class="cmd-desc">Get a link to your offer history</span></div>
      <div class="cmd-row"><span class="cmd-key">STATUS</span><span class="cmd-desc">Check your plan and usage</span></div>
      <div class="cmd-row"><span class="cmd-key">PROFILE</span><span class="cmd-desc">Edit your agent info (name, license, brokerage)</span></div>
      <div class="cmd-row"><span class="cmd-key">HELP</span><span class="cmd-desc">Text this menu back to yourself</span></div>
      <div class="cmd-row"><span class="cmd-key">STOP</span><span class="cmd-desc">Unsubscribe from all messages</span></div>
    </div>

    <div class="trust">
      <div class="trust-item"><div class="trust-val">&lt;10s</div><div class="trust-label">Generation</div></div>
      <div class="trust-item"><div class="trust-val">45 min</div><div class="trust-label">Saved per offer</div></div>
      <div class="trust-item"><div class="trust-val">TREC</div><div class="trust-label">20-19 Compliant</div></div>
      <div class="trust-item"><div class="trust-val">AES-256</div><div class="trust-label">Encrypted at rest</div></div>
    </div>

    <div class="foot">
      By texting or using this service, you consent to receive SMS responses. Reply STOP to opt out anytime. Msg &amp; data rates may apply.
      <br><a href="/pricing">View Pricing</a> &middot; <a href="/terms">Terms</a> &middot; <a href="/privacy">Privacy</a>
    </div>
  </div>
</body>
</html>
"""


@app.route("/demo", methods=["GET", "POST"])
def demo():
    result_html = ""
    prefill = ""
    date_stamp = datetime.now().strftime("%m/%d/%Y")

    if request.method == "POST":
        offer_text = request.form.get("offer_text", "")
        prefill = offer_text

        if _is_help_keyword(offer_text.strip().upper()):
            help_html = SMS_HELP_TEXT.replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
            result_html = f'<div class="result"><div class="result-stamp">Commands</div><div style="line-height:1.8;color:var(--text-muted);">{help_html}</div></div>'
            return DEMO_FORM.format(result_html=result_html, prefill=prefill, date_stamp=date_stamp)

        if offer_text.strip().upper().startswith("AMEND "):
            amend = parse_amendment_sms(offer_text)
            if "error" in amend:
                result_html = f'<div class="error">{amend["error"]}</div>'
            else:
                offer = find_recent_offer("demo-web", amend["address"])
                if not offer:
                    result_html = f'<div class="error">No demo offer found matching "{amend["address"]}". Generate an offer for that address above first, then amend it.</div>'
                else:
                    try:
                        pdf_path = fill_amendment_pdf(offer, amend)
                    except Exception as e:
                        result_html = f'<div class="error">Couldn\'t generate amendment: {e}</div>'
                    else:
                        filename = os.path.basename(pdf_path)
                        record_amendment(offer["id"], "demo-web", amend["field"], amend["value"], filename)
                        pdf_url = sign_pdf_url(filename)
                        _pdf_expires = int(time.time()) + PDF_LINK_TTL
                        _pdf_sig = hmac.new(PDF_LINK_SECRET.encode(), f"{filename}:{_pdf_expires}".encode(), hashlib.sha256).hexdigest()[:16]
                        change_line = f"New Sales Price: ${amend['value']:,}" if amend["field"] == "price" else f"Closing extended {amend['value']} days"
                        result_html = f"""
                        <div class="result">
                          <div class="result-stamp">Amendment (TREC 39-11)</div>
                          <div class="result-addr">{offer['address']}</div>
                          <div class="result-row"><span class="k">Change</span><span class="v">{change_line}</span></div>
                          <div class="result-ready">Ready for review.</div>
                          <div class="pdf-preview">
                            <div class="pdf-preview-label">Amendment preview</div>
                            <iframe src="/offers/{filename}?expires={_pdf_expires}&sig={_pdf_sig}#page=1&view=FitV" class="pdf-frame"></iframe>
                            <div class="pdf-mobile"><a href="{pdf_url}" target="_blank">Tap to view your amendment &rarr;</a></div>
                          </div>
                          <a href="/offers/{filename}?expires={_pdf_expires}&sig={_pdf_sig}" target="_blank" class="download-btn" download>&darr; Download PDF</a>
                        </div>
                        """
            return DEMO_FORM.format(result_html=result_html, prefill=prefill, date_stamp=date_stamp)

        if offer_text.strip().upper() in ("YES", "Y", "CONFIRM", "CREATE"):
            draft = get_draft("demo-web")
            if not draft:
                result_html = '<div class="error">No pending offer to confirm. Enter offer details above first.</div>'
                return DEMO_FORM.format(result_html=result_html, prefill=prefill, date_stamp=date_stamp)
            # Same guard as finalize_offer_sms -- this route calls
            # fill_offer_pdf directly and doesn't go through that choke
            # point, so it needs its own check against generating a PDF for
            # a still-unconfirmed-state draft.
            if draft.get("_tx_needs_confirm"):
                result_html = f'<div class="error">{TX_NEEDS_STATE_MESSAGE}</div>'
                return DEMO_FORM.format(result_html=result_html, prefill=prefill, date_stamp=date_stamp)
            try:
                pdf_path = fill_offer_pdf(draft, "demo-web")
            except Exception as e:
                result_html = f'<div class="error">Couldn\'t generate the PDF: {e}</div>'
                return DEMO_FORM.format(result_html=result_html, prefill=prefill, date_stamp=date_stamp)
            clear_draft("demo-web")
            parsed, error, warnings = draft, None, []
        elif offer_text.strip().upper() in ("NO", "CANCEL"):
            if get_draft("demo-web"):
                clear_draft("demo-web")
                result_html = '<div class="result"><div class="result-stamp">Cancelled</div><p style="color:var(--text-dim);">Offer cancelled. Enter new details above anytime.</p></div>'
            else:
                result_html = '<div class="error">Nothing pending to cancel.</div>'
            return DEMO_FORM.format(result_html=result_html, prefill=prefill, date_stamp=date_stamp)
        else:
            parsed, error, warnings = build_offer_draft(offer_text, "demo-web")
            if not error:
                save_draft("demo-web", parsed)
                if TX_UNVERIFIED_WARNING in warnings:
                    tx_html = format_tx_confirmation(parsed).replace("\n", "<br>")
                    result_html = f'<div class="result"><div class="result-stamp">Confirm Offer</div><div style="line-height:1.8;color:var(--text-muted);">{tx_html}</div></div>'
                    return DEMO_FORM.format(result_html=result_html, prefill=prefill, date_stamp=date_stamp)
                summary_html = format_offer_confirmation(parsed).replace("\n", "<br>")
                result_html = f'<div class="result"><div class="result-stamp">Confirm Offer</div><div style="line-height:1.8;color:var(--text-muted);">{summary_html}</div></div>'
                return DEMO_FORM.format(result_html=result_html, prefill=prefill, date_stamp=date_stamp)

            existing_draft = get_draft("demo-web")
            if existing_draft:
                correction = parse_correction_sms(offer_text)
                if correction:
                    existing_draft.update(correction)
                    if "price" in correction or "down_payment_pct" in correction:
                        agent = existing_draft.get("agent") or {}
                        existing_draft["down_payment_amount"] = int(existing_draft["price"] * existing_draft["down_payment_pct"])
                        existing_draft["loan_amount"] = existing_draft["price"] - existing_draft["down_payment_amount"]
                        existing_draft["earnest_money"] = int(existing_draft["price"] * agent.get("default_earnest_pct", 0.01))
                    save_draft("demo-web", existing_draft)
                    summary_html = format_offer_confirmation(existing_draft).replace("\n", "<br>")
                    result_html = f'<div class="result"><div class="result-stamp">Confirm Offer</div><div style="line-height:1.8;color:var(--text-muted);">{summary_html}</div></div>'
                else:
                    result_html = '<div class="error">Didn\'t catch a change. Try something like "make it 820k" or "close in 25 days", or reply YES / NO.</div>'
                return DEMO_FORM.format(result_html=result_html, prefill=prefill, date_stamp=date_stamp)

        # Reached only via the YES-confirm branch above (parsed/pdf_path set,
        # error=None), or the new-offer-draft branch's error case.
        if error:
            result_html = f'<div class="error">{error}</div>'
        else:
            filename = os.path.basename(pdf_path)
            record_offer("demo-web", parsed, filename)
            pdf_url = sign_pdf_url(filename)
            _pdf_expires = int(time.time()) + PDF_LINK_TTL
            _pdf_sig = hmac.new(PDF_LINK_SECRET.encode(), f"{filename}:{_pdf_expires}".encode(), hashlib.sha256).hexdigest()[:16]
            close_date_str = ""
            try:
                close_dt = datetime.now()
                from datetime import timedelta
                close_date_str = (close_dt + timedelta(days=parsed["close_days"])).strftime("%B %d, %Y")
            except Exception:
                close_date_str = f"{parsed['close_days']} days"
            warning_html = ""
            if warnings:
                warning_html = f'<div class="warning-note"><div class="wn-title">Review needed</div>{"<br>".join(warnings)}</div>'
            # Serialize parsed data for integration JS (strip non-serializable agent dict)
            import json as _json
            _parsed_safe = {k: v for k, v in parsed.items() if k != "agent"}
            parsed_json = _json.dumps(_parsed_safe)

            # Social share URLs
            share_text = "Just generated a TREC 20-19 contract in 3 seconds by texting an address 🤯 TxtAnOffer turns '725k 3% 21day 123 Main St' into a filled PDF instantly."
            share_url = "https://txtanoffer.com/demo"
            twitter_share = f"https://twitter.com/intent/tweet?text={share_text.replace(' ', '%20')}&url={share_url}"
            linkedin_share = f"https://www.linkedin.com/sharing/share-offsite/?url={share_url}"

            result_html = f"""
            <div class="result">
              <div class="result-stamp">Offer Summary</div>
              <div class="result-addr">{parsed['address']}</div>
              <div class="result-row"><span class="k">Purchase price</span><span class="v">${parsed['price']:,}</span></div>
              <div class="result-row"><span class="k">Down payment</span><span class="v">{parsed['down_payment_pct']*100:.0f}%</span></div>
              <div class="result-row"><span class="k">Closing</span><span class="v">{close_date_str}</span></div>
              {'<div class="result-row"><span class="k">Property</span><span class="v">' + ' · '.join([x for x in [f"{parsed.get('bed')} Bed" if parsed.get('bed') else '', f"{parsed.get('bath')} Bath" if parsed.get('bath') else '', f"{parsed.get('sqft'):,} Sqft" if parsed.get('sqft') else '', f"Built {parsed.get('year_built')}" if parsed.get('year_built') else ''] if x]) + '</span></div>' if parsed.get('bed') or parsed.get('sqft') else ''}
              <div class="result-ready">Ready for review.</div>
              {warning_html}
              <div class="pdf-preview">
                <div class="pdf-preview-label">Contract preview</div>
                <iframe src="/offers/{filename}?expires={_pdf_expires}&sig={_pdf_sig}#page=1&view=FitV" class="pdf-frame"></iframe>
                <div class="pdf-mobile"><a href="{pdf_url}" target="_blank">Tap to view your completed TREC 20-19 &rarr;</a></div>
              </div>
              <a href="/offers/{filename}?expires={_pdf_expires}&sig={_pdf_sig}" target="_blank" class="download-btn" download>&darr; Download PDF</a>
              <div class="integration-actions">
                <button class="int-btn int-email" onclick="document.getElementById('email-modal').style.display='flex'">&#9993; Email offer</button>
                <button class="int-btn int-docusign" onclick="document.getElementById('docusign-modal').style.display='flex'">&#9998; Send to DocuSign</button>
                <button class="int-btn int-webhook" onclick="document.getElementById('webhook-modal').style.display='flex'">&#9889; Webhook / Zapier</button>
              </div>

              <div id="email-modal" class="modal" style="display:none">
                <div class="modal-box">
                  <div class="modal-title">Email this offer</div>
                  <input type="email" id="email-to" placeholder="recipient@example.com" class="modal-input">
                  <button class="modal-submit" onclick="sendEmail('{filename}')">Send</button>
                  <div id="email-status" class="modal-status"></div>
                  <button class="modal-close" onclick="this.closest('.modal').style.display='none'">&times;</button>
                </div>
              </div>

              <div id="docusign-modal" class="modal" style="display:none">
                <div class="modal-box">
                  <div class="modal-title">Send for signature</div>
                  <input type="text" id="ds-name" placeholder="Signer full name" class="modal-input">
                  <input type="email" id="ds-email" placeholder="Signer email" class="modal-input">
                  <button class="modal-submit" onclick="sendDocuSign('{filename}')">Send via DocuSign</button>
                  <div id="ds-status" class="modal-status"></div>
                  <button class="modal-close" onclick="this.closest('.modal').style.display='none'">&times;</button>
                </div>
              </div>

              <div id="webhook-modal" class="modal" style="display:none">
                <div class="modal-box">
                  <div class="modal-title">Webhook / Zapier</div>
                  <p class="modal-desc">POST offer data to your CRM, Zapier, or any URL.</p>
                  <input type="url" id="wh-url" placeholder="https://hooks.zapier.com/..." class="modal-input">
                  <button class="modal-submit" onclick="configWebhook()">Save webhook</button>
                  <div id="wh-status" class="modal-status"></div>
                  <button class="modal-close" onclick="this.closest('.modal').style.display='none'">&times;</button>
                </div>
              </div>

              <script>
              function sendEmail(filename) {{
                const to = document.getElementById('email-to').value;
                const status = document.getElementById('email-status');
                if (!to) {{ status.textContent = 'Enter an email address'; return; }}
                status.textContent = 'Sending...';
                fetch('/api/send-email', {{
                  method: 'POST',
                  headers: {{'Content-Type': 'application/json'}},
                  body: JSON.stringify({{to_email: to, pdf_filename: filename, parsed: {parsed_json}, expires: '{_pdf_expires}', sig: '{_pdf_sig}'}})
                }}).then(r => r.json()).then(d => {{
                  status.textContent = d.success ? 'Sent!' : ('Error: ' + d.error);
                  status.className = 'modal-status ' + (d.success ? 'success' : 'fail');
                }}).catch(e => {{ status.textContent = 'Network error'; }});
              }}

              function sendDocuSign(filename) {{
                const name = document.getElementById('ds-name').value;
                const email = document.getElementById('ds-email').value;
                const status = document.getElementById('ds-status');
                if (!name || !email) {{ status.textContent = 'Name and email required'; return; }}
                status.textContent = 'Sending to DocuSign...';
                fetch('/api/docusign', {{
                  method: 'POST',
                  headers: {{'Content-Type': 'application/json'}},
                  body: JSON.stringify({{pdf_filename: filename, signer_email: email, signer_name: name, parsed: {parsed_json}, expires: '{_pdf_expires}', sig: '{_pdf_sig}'}})
                }}).then(r => r.json()).then(d => {{
                  status.textContent = d.success ? 'Sent! Envelope: ' + d.envelope_id : ('Error: ' + d.error);
                  status.className = 'modal-status ' + (d.success ? 'success' : 'fail');
                }}).catch(e => {{ status.textContent = 'Network error'; }});
              }}

              function configWebhook() {{
                const url = document.getElementById('wh-url').value;
                const status = document.getElementById('wh-status');
                if (!url) {{ status.textContent = 'Enter a webhook URL'; return; }}
                status.textContent = 'Saving...';
                fetch('/api/webhook', {{
                  method: 'POST',
                  headers: {{'Content-Type': 'application/json'}},
                  body: JSON.stringify({{source_id: 'demo-web', url: url, filename: '{filename}', expires: '{_pdf_expires}', sig: '{_pdf_sig}'}})
                }}).then(r => r.json()).then(d => {{
                  status.textContent = d.success ? 'Webhook saved! Future offers will POST here.' : ('Error: ' + (d.error || ''));
                  status.className = 'modal-status ' + (d.success ? 'success' : 'fail');
                }}).catch(e => {{ status.textContent = 'Network error'; }});
              }}
              </script>

              <div class="disclaimer">Draft only -- agent must review before signing. TREC NO. 20-19 (mandatory as of {TREC_FORM_CURRENT_AS_OF}).</div>

              <div class="share-section">
                <span class="share-label">Save 45 minutes per offer</span>
                <div class="share-buttons">
                  <a href="{twitter_share}" target="_blank" class="share-btn share-twitter">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg>
                    Tweet
                  </a>
                  <a href="{linkedin_share}" target="_blank" class="share-btn share-linkedin">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433c-1.144 0-2.063-.926-2.063-2.065 0-1.138.92-2.063 2.063-2.063 1.14 0 2.064.925 2.064 2.063 0 1.139-.925 2.065-2.064 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/></svg>
                    Share
                  </a>
                  <button class="share-btn share-copy" onclick="
                    navigator.clipboard.writeText('{share_url}');
                    this.textContent='✓ Copied!';
                    this.classList.add('copied');
                    setTimeout(()=>{{this.textContent='🔗 Copy link';this.classList.remove('copied');}},2000)
                  ">🔗 Copy link</button>
                </div>
              </div>
            </div>
            """

    return DEMO_FORM.format(prefill=prefill, result_html=result_html, date_stamp=date_stamp)


@app.route("/api/demo", methods=["POST"])
def api_demo():
    data = request.get_json()
    if not data or not data.get("offer_text"):
        return jsonify({"error": "Please enter offer details."}), 400
    offer_text = data["offer_text"].strip()
    parsed, pdf_path, error, warnings = process_offer(offer_text, "landing-demo")
    if error:
        return jsonify({"error": error}), 400
    filename = os.path.basename(pdf_path)
    record_offer("landing-demo", parsed, filename)
    pdf_url = sign_pdf_url(filename, request.host_url.rstrip("/"))
    from datetime import timedelta
    close_date = (datetime.now() + timedelta(days=parsed["close_days"])).strftime("%B %d, %Y")
    return jsonify({
        "address": parsed["address"],
        "price": parsed["price"],
        "down_pct": round(parsed["down_payment_pct"] * 100),
        "close_date": close_date,
        "pdf_url": pdf_url,
    })


@app.route("/api/parse", methods=["POST"])
def api_parse():
    """Parse-only endpoint for the playground — no PDF generated."""
    data = request.get_json()
    if not data or not data.get("text"):
        return jsonify({"error": "Please enter offer text."}), 400
    text = data["text"].strip()
    parsed = parse_offer_sms(text)
    if "error" in parsed:
        return jsonify({"success": False, "error": parsed["error"]}), 400
    addr_check = validate_address(parsed.get("address", ""), raw_text=text)
    address_issue = addr_check["reason"] if not addr_check["valid"] else (
        addr_check["warnings"][0] if addr_check["warnings"] else None
    )
    from datetime import timedelta
    close_date = (datetime.now() + timedelta(days=parsed["close_days"])).strftime("%B %d, %Y")
    down_amt = int(parsed["price"] * parsed["down_payment_pct"])
    loan_amt = parsed["price"] - down_amt
    return jsonify({
        "success": True,
        "price": parsed["price"],
        "down_payment_pct": round(parsed["down_payment_pct"] * 100, 1),
        "down_payment_amount": down_amt,
        "loan_amount": loan_amt,
        "close_days": parsed["close_days"],
        "close_date": close_date,
        "address": parsed["address"],
        "county": parsed.get("county", ""),
        "city": parsed.get("city", ""),
        "address_valid": addr_check["valid"],
        "address_issue": address_issue,
        "financing_type": parsed.get("financing_type"),
        "inspection_days": parsed.get("inspection_days"),
        "has_hoa": parsed.get("has_hoa", False),
    })


@app.route("/v1/tc/check", methods=["POST"])
def tc_check():
    """Transaction-coordinator file audit: upload a TREC 20-19 AcroForm PDF,
    get back which already-rect-verified fields are still blank. No auth in
    v1 (see MAX_CONTENT_LENGTH above for the abuse guard on an open upload
    endpoint) -- see tc_audit.py for exactly what is and isn't checked."""
    # Per-IP throttle: this endpoint shares a Railway service (and worker
    # processes) with the paying SMS product, so an unauthenticated flood
    # here could degrade that too, not just this free tool. This applies
    # regardless of the free-use/email gate below -- it's abuse protection,
    # not a product limit.
    client_ip = request.remote_addr or "unknown"
    if not check_and_increment(f"tc_check:{client_ip}", limit=20):
        return jsonify({"error": "Too many requests. Try again in a bit."}), 429

    run_tc_followup_if_due()

    # Product gate: the tool itself (running a check) is never limited --
    # only the ITEMIZED report is. Every upload gets a real summary ("4
    # issues found"); the actual per-field checklist requires an email,
    # every time, until one is on file for this browser (tracked by an
    # httponly client-id cookie, not IP -- shared offices/NAT would
    # otherwise share one visitor's state). See tc_gate.py.
    cid = request.cookies.get("tc_cid") or str(uuid.uuid4())
    client = get_tc_client(cid)
    submitted_email = (request.form.get("email") or "").strip()

    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"error": "No file uploaded. Attach a PDF as 'file'."}), 400
    if not upload.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported."}), 400

    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        upload.save(tmp.name)
        try:
            result = check_tc_file(tmp.name)
        except Exception:
            return jsonify({"error": "Couldn't read that file as a PDF. Make sure it's not corrupted or password-protected."}), 400

    record_tc_use(cid)
    # Deduped per file -- initials/addendum checks can fire multiple times
    # per file (once per page), and issue_frequency's "% of recognized
    # files" in analytics.py only means what it says if each file counts
    # once per issue type, not once per occurrence.
    issue_keys = sorted({i["key"] for i in result["issues"] if i.get("key")})
    track_event("tc_check", metadata={
        "recognized": result["recognized"],
        "complete": result["complete"],
        "issue_keys": issue_keys,
    })

    email_just_captured = False
    if not client["email"] and submitted_email and "@" in submitted_email:
        save_tc_email(cid, submitted_email)
        client["email"] = submitted_email
        email_just_captured = True
        track_event("tc_check_email_captured", submitted_email, {"client_id": cid})

    # Nothing to gate on a clean file or an unrecognized upload -- the
    # itemized list IS the product's value, so only withhold it when
    # there's actually something in it.
    has_itemized_content = result["recognized"] and not result["complete"]

    if has_itemized_content and not client["email"]:
        blockers = sum(1 for i in result["issues"] if i["severity"] == "blocker")
        warnings = sum(1 for i in result["issues"] if i["severity"] == "warning")
        track_event("tc_check_gated", metadata={"issue_count": len(result["issues"])})
        resp = jsonify({
            "recognized": result["recognized"],
            "complete": result["complete"],
            "page_count": result["page_count"],
            "has_addendum": result["has_addendum"],
            "looks_like_blank_draft": result["looks_like_blank_draft"],
            "issue_count": len(result["issues"]),
            "blocker_count": blockers,
            "warning_count": warnings,
            "email_required": True,
        })
    else:
        if email_just_captured:
            send_tc_nudge(submitted_email, result)
        resp = jsonify(result)
    resp.set_cookie("tc_cid", cid, max_age=365 * 24 * 3600, httponly=True, samesite="Lax")
    return resp


@app.route("/tc-check")
def tc_check_page():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TC File Check — TxtAnOffer</title>
<meta name="description" content="Drop a filled TREC 20-19 PDF and see what's missing before title kicks it back.">
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
:root{--bg:#F5F5F7;--bg-card:#fff;--border:rgba(15,31,47,0.08);
--text:#0f1f2f;--text-muted:#5a6b7a;--text-dim:#8a9aa9;--accent:#171717;--accent-light:#525252;
--accent-dark:#000000;--accent-tint:#F0F0EE;--radius:1.25rem;--radius-sm:0.85rem;}
*{margin:0;padding:0;box-sizing:border-box;}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;
-webkit-font-smoothing:antialiased;}
a{color:inherit;text-decoration:none;}
.nav{display:flex;align-items:center;justify-content:space-between;padding:1rem 2rem;
background:rgba(255,255,255,0.85);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);
position:sticky;top:0;z-index:100;}
.nav-left{display:flex;align-items:center;gap:0.6rem;font-weight:700;font-size:1.1rem;color:var(--text);}
.nav-logo{width:34px;height:34px;border-radius:22%;overflow:hidden;}
.nav-logo img{width:100%;height:100%;object-fit:contain;}
.container{max-width:700px;margin:0 auto;padding:3rem 2rem;}
h1{font-size:2rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.5rem;color:var(--text);}
.subtitle{color:var(--text-muted);font-size:1rem;margin-bottom:2rem;}
.card{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:2rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);}
.drop-zone{border:2px dashed rgba(15,31,47,0.18);border-radius:var(--radius-sm);padding:3rem 1.5rem;
text-align:center;cursor:pointer;transition:all 0.2s;}
.drop-zone:hover,.drop-zone.drag{border-color:var(--accent);background:var(--accent-tint);}
.drop-zone svg{margin-bottom:0.75rem;}
.drop-zone .dz-title{font-weight:700;font-size:1rem;margin-bottom:0.25rem;}
.drop-zone .dz-sub{color:var(--text-dim);font-size:0.85rem;}
.privacy-note{display:flex;align-items:center;gap:0.45rem;margin-top:0.9rem;font-size:0.8rem;color:var(--text-dim);}
.privacy-note svg{flex-shrink:0;}
input[type=file]{display:none;}
.status{margin-top:1.25rem;font-size:0.9rem;color:var(--text-muted);display:none;}
.status.show{display:block;}
.result{margin-top:1.5rem;display:none;}
.result.show{display:block;}
.result-banner{border-radius:var(--radius-sm);padding:1rem 1.25rem;font-weight:700;margin-bottom:1rem;}
.result-banner.complete{background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.25);color:#047857;}
.result-banner.incomplete{background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.2);color:#dc2626;}
.issue-list{list-style:none;margin-bottom:1.25rem;}
.issue-item{display:flex;gap:0.6rem;padding:0.65rem 0;border-bottom:1px solid var(--border);font-size:0.9rem;}
.issue-item:last-child{border-bottom:none;}
.issue-tag{flex-shrink:0;font-size:0.65rem;font-weight:700;text-transform:uppercase;letter-spacing:0.04em;
padding:0.15rem 0.5rem;border-radius:9999px;height:fit-content;}
.issue-tag.blocker{background:rgba(239,68,68,0.12);color:#dc2626;}
.issue-tag.warning{background:rgba(245,158,11,0.12);color:#b45309;}
.copy-btn{background:var(--accent);color:#fff;border:none;padding:0.7rem 1.5rem;border-radius:var(--radius-sm);
font-family:inherit;font-size:0.85rem;font-weight:600;cursor:pointer;}
.copy-btn:hover{opacity:0.9;}
.fixit-cta{margin-top:1.25rem;padding:1.1rem 1.25rem;background:var(--accent-tint);border:1px solid var(--border);
border-radius:var(--radius-sm);display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap;}
.fixit-cta p{font-size:0.85rem;color:var(--text-muted);margin:0;}
.fixit-cta a{background:var(--accent);color:#fff;padding:0.6rem 1.25rem;border-radius:9999px;
font-size:0.85rem;font-weight:600;white-space:nowrap;}
.fixit-cta a:hover{opacity:0.9;}
.meta-bar{display:flex;flex-wrap:wrap;gap:0.4rem 1.25rem;padding:0.85rem 1.1rem;margin-bottom:1rem;
background:var(--accent-tint);border:1px solid var(--border);border-radius:var(--radius-sm);
font-size:0.8rem;color:var(--text-muted);}
.meta-bar strong{color:var(--text);font-weight:600;}
.download-btn{background:#fff;color:var(--text);border:1px solid rgba(15,31,47,0.14);padding:0.7rem 1.5rem;
border-radius:var(--radius-sm);font-family:inherit;font-size:0.85rem;font-weight:600;cursor:pointer;margin-left:0.6rem;}
.download-btn:hover{border-color:var(--accent);}
.scope-card{margin-top:2rem;background:var(--accent-tint);border:1px solid var(--border);border-radius:var(--radius);padding:1.5rem 1.75rem;}
.scope-card h3{font-size:0.75rem;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:var(--text-dim);margin-bottom:0.9rem;}
.scope-grid{display:grid;grid-template-columns:1fr 1fr;gap:0.5rem 2rem;margin-bottom:1rem;}
.scope-grid ul{list-style:none;}
.scope-grid li{position:relative;padding-left:1.1rem;font-size:0.85rem;color:var(--text-muted);line-height:1.7;}
.scope-grid li::before{content:'\\2713';position:absolute;left:0;color:#0f9960;font-weight:700;}
.scope-grid .not-checked li::before{content:'\\2013';color:var(--text-dim);}
.scope-footnote{font-size:0.78rem;color:var(--text-dim);line-height:1.6;border-top:1px solid var(--border);padding-top:0.85rem;}
@media(max-width:600px){.scope-grid{grid-template-columns:1fr;}}
.checks-remaining{font-size:0.78rem;color:var(--text-dim);margin:-0.5rem 0 1rem;}
.email-gate-msg{font-size:0.95rem;font-weight:600;margin-bottom:1rem;}
.email-gate-form{display:flex;gap:0.6rem;flex-wrap:wrap;}
.email-gate-form input{flex:1;min-width:180px;padding:0.7rem 1rem;border:1px solid rgba(15,31,47,0.16);
border-radius:var(--radius-sm);font-family:inherit;font-size:0.9rem;background:#fff;color:var(--text);}
.email-gate-form input:focus{outline:none;border-color:var(--accent);}
.email-gate-fine{font-size:0.78rem;color:var(--text-dim);margin-top:0.75rem;}
</style>
</head>
<body>
<nav class="nav">
<a href="/" class="nav-left">
<div class="nav-logo"><img src="/static/logo.svg" alt="TxtAnOffer"></div>
<span>TxtAnOffer</span>
</a>
</nav>
<div class="container">
<h1>TC File Check</h1>
<p class="subtitle">Drop a filled TREC 20-19 PDF. We'll tell you what's missing before title kicks it back.</p>
<div class="card">
<div class="drop-zone" id="dropZone">
<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#8a9aa9" stroke-width="1.5"><path d="M12 16V4M12 4l-4 4M12 4l4 4" stroke-linecap="round" stroke-linejoin="round"/><path d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2" stroke-linecap="round" stroke-linejoin="round"/></svg>
<div class="dz-title">Drop a TREC PDF here, or click to choose</div>
<div class="dz-sub">AcroForm-fillable PDFs only &mdash; not scanned or flattened files</div>
</div>
<input type="file" id="fileInput" accept="application/pdf">
<div class="privacy-note"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>Processed instantly and never stored &mdash; your file is discarded the moment your results are ready.</div>
<div class="status" id="status"></div>
<div class="result" id="result"></div>
</div>
<div class="scope-card">
<h3>What this checks</h3>
<div class="scope-grid">
<ul>
<li>Property address, county</li>
<li>Buyer &amp; Seller legal name</li>
<li>Earnest money, option fee</li>
<li>Escrow agent, title company</li>
<li>Effective Date</li>
<li>Buyer/Seller initials on every page that requires them</li>
<li>40-11 addendum loan amount matches the contract</li>
<li>Third Party Financing checkboxes agree with whether an addendum is attached</li>
</ul>
<ul class="not-checked">
<li>Earnest-money receipts</li>
<li>Scanned or flattened PDFs</li>
</ul>
</div>
<p class="scope-footnote">Every check above is verified directly against TREC's actual 20-19 form fields &mdash; not guessed from field names, which routinely lie about their own position.</p>
</div>
</div>
<script>
const dropZone = document.getElementById('dropZone');
const fileInput = document.getElementById('fileInput');
const statusEl = document.getElementById('status');
const resultEl = document.getElementById('result');

dropZone.addEventListener('click', () => fileInput.click());
dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('drag');
  if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener('change', () => {
  if (fileInput.files.length) uploadFile(fileInput.files[0]);
});

// Purely a perceived-progress readout for a request that's actually one
// round trip -- the backend doesn't stream distinct stages back. Labeled
// generically (not "AI analyzing..." theater) and never blocks: whichever
// line is showing when the real response lands, that's when it finishes.
const STATUS_STEPS = ['Reading PDF...', 'Checking required fields...', 'Checking initials & consistency...'];
let statusTimers = [];
let pendingFile = null;

function uploadFile(file, email) {
  pendingFile = file;
  resultEl.classList.remove('show');
  statusTimers.forEach(clearTimeout);
  statusTimers = STATUS_STEPS.map((label, i) =>
    setTimeout(() => { statusEl.textContent = label; }, i * 450)
  );
  statusEl.textContent = STATUS_STEPS[0];
  statusEl.classList.add('show');

  const formData = new FormData();
  formData.append('file', file);
  if (email) formData.append('email', email);

  fetch('/v1/tc/check', { method: 'POST', body: formData })
    .then(r => r.json())
    .then(data => {
      statusTimers.forEach(clearTimeout);
      statusEl.classList.remove('show');
      if (data.error) {
        renderError(data.error);
        return;
      }
      if (data.email_required) {
        renderGatedSummary(data, file);
        return;
      }
      renderResult(data, file);
    })
    .catch(() => {
      statusTimers.forEach(clearTimeout);
      statusEl.classList.remove('show');
      renderError('Something went wrong checking that file. Try again.');
    });
}

function buildMetaBar(data, file) {
  let html = '<div class="meta-bar">';
  html += '<span><strong>' + escapeHtml(file.name) + '</strong></span>';
  html += '<span>' + formatBytes(file.size) + '</span>';
  if (typeof data.page_count === 'number') html += '<span>' + data.page_count + ' page' + (data.page_count === 1 ? '' : 's') + '</span>';
  html += '<span>' + (data.recognized ? 'TREC 20-19 AcroForm detected' : 'Not recognized as a TREC 20-19') + '</span>';
  if (data.has_addendum) html += '<span>40-11 addendum attached</span>';
  html += '</div>';
  return html;
}

function renderGatedSummary(data, file) {
  let html = buildMetaBar(data, file);
  html += '<div class="result-banner incomplete">' + data.issue_count + ' issue' + (data.issue_count === 1 ? '' : 's') + ' found';
  if (data.blocker_count) html += ' &mdash; ' + data.blocker_count + ' would get this file kicked back by title';
  html += '</div>';
  if (data.looks_like_blank_draft) {
    html += '<div class="fixit-cta"><p>This looks like an essentially blank draft &mdash; more gaps than a quick fix. It may be faster to generate a clean one from scratch.</p><a href="/demo">Generate a clean offer &rarr;</a></div>';
  }
  html += '<p class="email-gate-msg">Enter your email to see exactly which fields and sections &mdash; the full itemized checklist.</p>';
  html += '<form id="emailGateForm" class="email-gate-form">' +
    '<input type="email" id="emailGateInput" placeholder="you@brokerage.com" required>' +
    '<button type="submit" class="copy-btn">See full report</button>' +
    '</form>' +
    '<p class="email-gate-fine">No spam &mdash; just occasional product updates.</p>';
  resultEl.innerHTML = html;
  resultEl.classList.add('show');
  document.getElementById('emailGateForm').addEventListener('submit', e => {
    e.preventDefault();
    const email = document.getElementById('emailGateInput').value.trim();
    if (!email || !pendingFile) return;
    uploadFile(pendingFile, email);
  });
}

function formatBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  return (n / (1024 * 1024)).toFixed(1) + ' MB';
}

function renderError(msg) {
  resultEl.innerHTML = '<div class="result-banner incomplete">' + escapeHtml(msg) + '</div>';
  resultEl.classList.add('show');
}

function renderResult(data, file) {
  const issues = data.issues || [];
  let html = buildMetaBar(data, file);

  if (data.complete) {
    html += '<div class="result-banner complete">All checked fields are filled in.</div>';
  } else {
    html += '<div class="result-banner incomplete">' + issues.length + ' issue' + (issues.length === 1 ? '' : 's') + ' found</div>';
  }
  if (data.looks_like_blank_draft) {
    html += '<div class="fixit-cta"><p>This looks like an essentially blank draft &mdash; more gaps than a quick fix. It may be faster to generate a clean one from scratch.</p><a href="/demo">Generate a clean offer &rarr;</a></div>';
  }
  if (issues.length) {
    html += '<ul class="issue-list">';
    for (const issue of issues) {
      html += '<li class="issue-item"><span class="issue-tag ' + issue.severity + '">' + issue.severity + '</span><span>' + escapeHtml(issue.message) + '</span></li>';
    }
    html += '</ul>';
    html += '<button class="copy-btn" onclick="copyChecklist()">Copy checklist</button>';
    html += '<button class="download-btn" onclick="downloadReport()">Download report</button>';
    html += '<div class="fixit-cta"><p>Every gap above happened because this file was filled out by hand. TxtAnOffer drafts the 20-19 by text message, so these fields are never blank to begin with.</p><a href="/pricing">See how it works &rarr;</a></div>';
  }
  resultEl.innerHTML = html;
  resultEl.classList.add('show');
  resultEl.dataset.issues = JSON.stringify(issues);
  resultEl.dataset.filename = file.name;
}

function checklistText() {
  const issues = JSON.parse(resultEl.dataset.issues || '[]');
  return issues.map(i => '- [' + i.severity.toUpperCase() + '] ' + i.message).join('\\n');
}

function copyChecklist() {
  navigator.clipboard.writeText(checklistText());
}

function downloadReport() {
  const filename = resultEl.dataset.filename || 'file.pdf';
  const report = 'TC File Check report\\n' +
    'File: ' + filename + '\\n' +
    'Checked: ' + new Date().toLocaleString() + '\\n' +
    'txtanoffer.com/tc-check\\n\\n' +
    checklistText() + '\\n';
  const blob = new Blob([report], { type: 'text/plain' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename.replace(/\\.pdf$/i, '') + '-tc-check-report.txt';
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s;
  return div.innerHTML;
}
</script>
</body>
</html>"""
    # Same ?src= first-touch attribution pattern as the homepage (see "/"
    # route above) -- reuses the same ta_src cookie, so a tc-check visit
    # that later leads to a signup still attributes correctly even though
    # it happened on a different page/day. Without this, every outreach
    # link into /tc-check was untracked: no way to tell whether a channel
    # (LinkedIn, a DM) drove any traffic here at all.
    import re as _re
    src = _re.sub(r"[^a-zA-Z0-9_-]", "", request.args.get("src", ""))[:60]
    resp = make_response(html)
    if src and not request.cookies.get("ta_src"):
        resp.set_cookie("ta_src", src, max_age=30 * 24 * 3600, httponly=True, samesite="Lax")
        track_event("landing_visit", None, {"source": src})
    return resp


@app.route("/playground")
def playground():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Parser Playground — TxtAnOffer</title>
<meta name="description" content="Test the TxtAnOffer SMS parser. See how messy texts become structured TREC offers in real-time.">
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
:root{--bg:#F5F5F7;--bg-card:#fff;--border:rgba(15,31,47,0.08);
--text:#0f1f2f;--text-muted:#5a6b7a;--text-dim:#8a9aa9;--accent:#171717;--accent-light:#525252;
--accent-dark:#000000;--accent-tint:#F0F0EE;--radius:1.25rem;--radius-sm:0.85rem;}
*{margin:0;padding:0;box-sizing:border-box;}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;
-webkit-font-smoothing:antialiased;}
a{color:inherit;text-decoration:none;}
.nav{display:flex;align-items:center;justify-content:space-between;padding:1rem 2rem;
background:rgba(255,255,255,0.85);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);
position:sticky;top:0;z-index:100;}
.nav-left{display:flex;align-items:center;gap:0.6rem;font-weight:700;font-size:1.1rem;color:var(--text);}
.nav-logo{width:34px;height:34px;border-radius:22%;overflow:hidden;}
.nav-logo img{width:100%;height:100%;object-fit:contain;}
.nav-links{display:flex;gap:2rem;font-size:0.875rem;font-weight:500;color:var(--text-muted);}
.nav-links a:hover{color:var(--text);}
.nav-cta{background:var(--accent);color:#fff;padding:0.55rem 1.35rem;border-radius:9999px;
font-size:0.875rem;font-weight:600;}
.nav-toggle{display:none;flex-direction:column;justify-content:center;gap:5px;width:34px;height:34px;background:none;border:none;cursor:pointer;padding:0;}
.nav-toggle span{display:block;width:100%;height:2px;background:var(--text);border-radius:2px;}
.container{max-width:900px;margin:0 auto;padding:3rem 2rem;}
h1{font-size:2rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.5rem;color:var(--text);}
.subtitle{color:var(--text-muted);font-size:1rem;margin-bottom:2rem;}
.playground-card{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:2rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);}
.input-area{margin-bottom:1.5rem;}
.input-area label{display:block;font-size:0.8rem;font-weight:600;color:var(--text-dim);
text-transform:uppercase;letter-spacing:0.05em;margin-bottom:0.5rem;}
.input-area textarea{width:100%;background:#fff;border:1px solid rgba(15,31,47,0.14);
border-radius:var(--radius-sm);color:var(--text);font-family:inherit;font-size:1rem;
padding:1rem;resize:none;outline:none;transition:border 0.2s;}
.input-area textarea:focus{border-color:var(--accent);}
.parse-btn{background:linear-gradient(135deg,var(--accent),#000);color:#fff;border:none;
padding:0.85rem 2rem;border-radius:var(--radius-sm);font-family:inherit;font-size:0.9rem;
font-weight:600;cursor:pointer;transition:all 0.2s;}
.parse-btn:hover{transform:translateY(-2px);box-shadow:0 8px 24px rgba(0,0,0,0.25);}
.result{margin-top:1.5rem;display:none;}
.result.show{display:block;}
.result-grid{display:grid;grid-template-columns:1fr 1fr;gap:0.75rem;}
.result-item{background:rgba(15,31,47,0.02);border:1px solid var(--border);
border-radius:var(--radius-sm);padding:1rem;}
.result-label{font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;
color:var(--text-dim);margin-bottom:0.25rem;}
.result-value{font-size:1.1rem;font-weight:700;color:var(--text);}
.result-value.accent{color:var(--accent-dark);}
.error-msg{background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.2);
border-radius:var(--radius-sm);padding:1rem;color:#dc2626;font-size:0.9rem;margin-top:1rem;display:none;}
.error-msg.show{display:block;}
.warn-msg{background:rgba(245,158,11,0.1);border:1px solid rgba(245,158,11,0.25);
border-radius:var(--radius-sm);padding:1rem;color:#b45309;font-size:0.9rem;margin-bottom:1rem;display:none;}
.warn-msg.show{display:block;}
.examples{margin-top:2rem;}
.examples h3{font-size:0.9rem;font-weight:700;margin-bottom:1rem;color:var(--text-muted);}
.example-chips{display:flex;flex-wrap:wrap;gap:0.5rem;}
.chip{background:rgba(15,31,47,0.03);border:1px solid var(--border);border-radius:9999px;
padding:0.4rem 0.85rem;font-size:0.8rem;color:var(--text-muted);cursor:pointer;transition:all 0.2s;}
.chip:hover{border-color:var(--accent);color:var(--accent-dark);}
.formats{margin-top:2.5rem;padding-top:2rem;border-top:1px solid var(--border);}
.formats h3{font-size:1rem;font-weight:700;margin-bottom:1rem;color:var(--text);}
.format-grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem;}
.format-item{font-size:0.85rem;color:var(--text-muted);line-height:1.6;}
.format-item strong{color:var(--text);font-weight:600;}
@media(max-width:600px){
.result-grid{grid-template-columns:1fr;}
.format-grid{grid-template-columns:1fr;}
.nav-toggle{display:flex;}
.nav-links{display:none;position:absolute;top:100%;left:0;right:0;flex-direction:column;gap:0;padding:0.5rem 1.25rem 1.25rem;background:#fff;border-bottom:1px solid rgba(15,31,47,0.08);}
.nav-links.open{display:flex;}
.nav-links a{padding:0.75rem 0;border-bottom:1px solid rgba(15,31,47,0.08);}
.nav-links a:last-child{border-bottom:none;}
}
</style>
</head>
<body>
<nav class="nav">
<a href="/" class="nav-left">
<div class="nav-logo"><img src="/static/logo.svg" alt="TxtAnOffer"></div>
<span>TxtAnOffer</span>
</a>
<div class="nav-links" id="navLinks">
<a href="/#how">How it works</a>
<a href="/#trust">Accuracy</a>
<a href="/pricing">Pricing</a>
<a href="/demo">Demo</a>
<a href="/playground">Parser Playground</a>
<a href="/faq">FAQ</a>
<a href="/about">About</a>
<a href="/contact">Support</a>
<a href="/login">Log In</a>
</div>
<a href="/signup" class="nav-cta">Start Free Trial</a>
<button class="nav-toggle" id="navToggle" aria-label="Menu" aria-expanded="false"><span></span><span></span><span></span></button>
</nav>
<script>
(function(){
  var t=document.getElementById('navToggle'), l=document.getElementById('navLinks');
  if(!t||!l) return;
  t.addEventListener('click', function(){
    var open = l.classList.toggle('open');
    t.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  l.querySelectorAll('a').forEach(function(a){
    a.addEventListener('click', function(){ l.classList.remove('open'); t.setAttribute('aria-expanded','false'); });
  });
})();
</script>

<div class="container">
<h1>Parser Playground</h1>
<p class="subtitle">Test how our parser handles your texts. No signup needed. Type however feels natural.</p>

<div class="playground-card">
<div class="input-area">
<label>Your offer text</label>
<textarea id="offer-input" rows="3" placeholder="725k 3% 21day 123 Main St, Austin"></textarea>
</div>
<button class="parse-btn" id="parse-btn">Parse &rarr;</button>

<div class="error-msg" id="error-msg"></div>

<div class="result" id="result">
<div class="warn-msg" id="warn-msg"></div>
<div class="result-grid">
<div class="result-item"><div class="result-label">Address</div><div class="result-value" id="r-addr"></div></div>
<div class="result-item"><div class="result-label">Sales Price</div><div class="result-value accent" id="r-price"></div></div>
<div class="result-item"><div class="result-label">Down Payment</div><div class="result-value" id="r-down"></div></div>
<div class="result-item"><div class="result-label">Loan Amount</div><div class="result-value" id="r-loan"></div></div>
<div class="result-item"><div class="result-label">Closing Date</div><div class="result-value" id="r-close"></div></div>
<div class="result-item"><div class="result-label">Location</div><div class="result-value" id="r-location"></div></div>
<div class="result-item" style="grid-column:1 / -1;"><div class="result-label">Extras Detected</div><div class="result-value" id="r-extras"></div></div>
</div>
</div>

<div class="examples">
<h3>Try these (click to load):</h3>
<div class="example-chips">
<span class="chip">725k 3% 21day 123 Main St</span>
<span class="chip">Offer 650000 3 percent close in 30 days 456 Oak St Austin</span>
<span class="chip">500k 5 down 14days 200 Preston Rd Plano</span>
<span class="chip">1.2m 10% 45day Travis 789 Pine Blvd</span>
<span class="chip">825k 3% close in 14 1900 Exposition Blvd</span>
<span class="chip">375,000 3% 30days 2100 South Congress Ave</span>
<span class="chip">725k cash 21day 123 Main St</span>
<span class="chip">725k 3% 21day 123 Main St HOA</span>
</div>
</div>

<div class="formats">
<h3>We handle messy texts. Just get the numbers in there.</h3>
<div class="format-grid">
<div class="format-item"><strong>Price:</strong> 725k, 725000, 725,000, 1.2m, 1.2mil</div>
<div class="format-item"><strong>Down:</strong> 3%, 3 percent, 3 pct, 3 down</div>
<div class="format-item"><strong>Days:</strong> 21day, 21 days, close in 21, 21-day close</div>
<div class="format-item"><strong>Address:</strong> Just include street number + name + type</div>
</div>
</div>
</div>
</div>

<script>
(function(){
var input=document.getElementById('offer-input'),
    btn=document.getElementById('parse-btn'),
    result=document.getElementById('result'),
    errEl=document.getElementById('error-msg'),
    warnEl=document.getElementById('warn-msg');

document.querySelectorAll('.chip').forEach(function(c){
  c.addEventListener('click',function(){
    input.value=c.textContent;
    btn.click();
  });
});

btn.addEventListener('click',function(){
  var text=input.value.trim();
  if(!text)return;
  result.classList.remove('show');
  errEl.classList.remove('show');
  warnEl.classList.remove('show');
  fetch('/api/parse',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text:text})})
  .then(function(r){return r.json();})
  .then(function(d){
    if(!d.success){errEl.textContent=d.error;errEl.classList.add('show');return;}
    document.getElementById('r-addr').textContent=d.address;
    document.getElementById('r-price').textContent='$'+d.price.toLocaleString();
    document.getElementById('r-down').textContent=d.down_payment_pct+'% ($'+d.down_payment_amount.toLocaleString()+')';
    document.getElementById('r-loan').textContent='$'+d.loan_amount.toLocaleString();
    document.getElementById('r-close').textContent=d.close_date+' ('+d.close_days+' days)';
    var loc=[];if(d.city)loc.push(d.city);if(d.county)loc.push(d.county+' County');loc.push('TX');
    document.getElementById('r-location').textContent=loc.join(', ');
    var extras=[];
    if(d.financing_type)extras.push(d.financing_type.toUpperCase()+' financing');
    if(d.inspection_days)extras.push(d.inspection_days+'-day option period');
    if(d.has_hoa)extras.push('HOA Addendum (TREC 36-10)');
    document.getElementById('r-extras').textContent=extras.length?extras.join(' · '):'None detected';
    if(d.address_issue){
      warnEl.textContent=(d.address_valid?'Heads up: ':'This address would be rejected when sent for real: ')+d.address_issue;
      warnEl.classList.add('show');
    }
    result.classList.add('show');
  })
  .catch(function(){errEl.textContent='Something went wrong.';errEl.classList.add('show');});
});

input.addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();btn.click();}});
})();
</script>
</body>
</html>"""


# --- Integration endpoints -------------------------------------------------

@app.route("/api/send-email", methods=["POST"])
def api_send_email():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "JSON body required"}), 400

    to_email = data.get("to_email", "")
    pdf_filename = data.get("pdf_filename", "")
    parsed = data.get("parsed", {})
    expires = data.get("expires", "")
    sig = data.get("sig", "")

    if not to_email or not pdf_filename:
        return jsonify({"success": False, "error": "to_email and pdf_filename required"}), 400

    # Auth: either bearer token OR valid PDF signature (from review page)
    has_bearer = False
    auth = request.headers.get("Authorization", "")
    if API_BEARER_TOKEN and auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], API_BEARER_TOKEN):
        has_bearer = True

    has_sig = verify_pdf_signature(pdf_filename, expires, sig)

    if not has_bearer and not has_sig:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    if ".." in pdf_filename or pdf_filename.startswith("/"):
        abort(400)

    pdf_path = os.path.join(OUTPUT_DIR, pdf_filename)
    if not os.path.exists(pdf_path):
        return jsonify({"success": False, "error": "PDF not found"}), 404

    # Server-side re-check: the review page disables the Email button when
    # this fails, but that's only a UI convenience -- don't trust it, since
    # this endpoint can be hit directly. Rebuild a fuller parsed dict from
    # the stored offer (the client only sends address/price) so consistency
    # checks (Section 3 vs. offer total, etc.) actually have something to
    # compare against.
    offer_row = get_offer_by_filename(pdf_filename)
    if offer_row and offer_row.get("price"):
        down_amt = int(offer_row["price"] * offer_row["down_pct"])
        validation_parsed = {
            "address": offer_row.get("address") or parsed.get("address", ""),
            "price": offer_row["price"], "down_payment_amount": down_amt,
            "loan_amount": offer_row["price"] - down_amt, "close_days": offer_row["close_days"],
            "created_at": offer_row.get("created_at"),
            "financing_type_specified": bool(offer_row.get("financing_type")),
        }
    else:
        validation_parsed = parsed
    validation = validate_offer_pdf(pdf_path, validation_parsed)
    if validation["blocking"]:
        return jsonify({
            "success": False,
            "error": "This contract is missing required fields and can't be sent: " + "; ".join(validation["blocking"]),
            "missing_fields": validation["blocking"],
        }), 422

    # Use the same reconstructed dict for the email body as for validation --
    # the raw client-submitted `parsed` only has address/price (see comment
    # above), so passing it here silently rendered "Close: N/A days" in every
    # listing-agent email regardless of the offer's real closing date. Caught
    # 2026-08-22 auditing the "nothing slips through" claim: found via a real
    # email that had actually gone out with a mismatched closing date.
    thread_url = sign_thread_url(pdf_filename, request.host_url.rstrip("/"))
    result = send_offer_email(to_email, pdf_path, validation_parsed, thread_url=thread_url)
    track_event("email_sent" if result["success"] else "email_failed", to_email, result)
    if result["success"]:
        record_email_sent(pdf_filename, to_email)
    return jsonify(result), 200 if result["success"] else 500


@app.route("/api/webhook", methods=["GET", "POST", "DELETE"])
def api_webhook():
    if request.method == "POST":
        data = request.get_json()
        if not data:
            return jsonify({"error": "JSON body required"}), 400
        # The review page's own "Webhook / Zapier" button calls this with no
        # bearer token available to it -- authorize via the same signed
        # filename/expires/sig it already has for the offer being viewed.
        # True server-to-server API callers still just send a Bearer token
        # and can omit these.
        auth_error = require_api_or_pdf_signature_auth(
            data.get("filename", ""), data.get("expires", ""), data.get("sig", "")
        )
        if auth_error:
            return auth_error
        source_id = data.get("source_id", "")
        url = data.get("url", "")
        if not source_id or not url:
            return jsonify({"error": "source_id and url required"}), 400
        if not has_professional_access(source_id):
            return jsonify({"error": "Webhook automation is a Professional-plan feature. Upgrade at txtanoffer.com/pricing."}), 403
        if not _is_safe_webhook_url(url):
            return jsonify({"error": "Invalid webhook URL (must be public HTTPS)"}), 400
        save_webhook(source_id, url)
        track_event("webhook_configured", source_id, {"url": url})
        return jsonify({"success": True, "source_id": source_id, "url": url})

    # GET and DELETE are server-to-server only (no browser UI calls these) --
    # bearer token required, no signature fallback.
    auth_error = require_api_auth()
    if auth_error:
        return auth_error

    if request.method == "GET":
        source_id = request.args.get("source_id", "")
        if not source_id:
            return jsonify({"error": "source_id required"}), 400
        url = get_webhook(source_id)
        return jsonify({"source_id": source_id, "url": url, "active": url is not None})

    if request.method == "DELETE":
        data = request.get_json()
        if not data:
            return jsonify({"error": "JSON body required"}), 400
        source_id = data.get("source_id", "")
        if not source_id:
            return jsonify({"error": "source_id required"}), 400
        delete_webhook(source_id)
        return jsonify({"success": True, "deleted": source_id})


@app.route("/api/docusign", methods=["POST"])
def api_docusign():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "JSON body required"}), 400

    pdf_filename = data.get("pdf_filename", "")
    parsed = data.get("parsed", {})
    signer_email = data.get("signer_email", "")
    signer_name = data.get("signer_name", "")

    # The review page's own "Send to DocuSign" button calls this with no
    # bearer token available to it -- authorize via the same signed
    # expires/sig it already has for the offer being viewed. True
    # server-to-server API callers still just send a Bearer token.
    auth_error = require_api_or_pdf_signature_auth(
        pdf_filename, data.get("expires", ""), data.get("sig", "")
    )
    if auth_error:
        return auth_error

    if not pdf_filename or not signer_email or not signer_name:
        return jsonify({"success": False, "error": "pdf_filename, signer_email, and signer_name required"}), 400

    owning_offer = get_offer_by_filename(pdf_filename)
    owning_phone = owning_offer["phone"] if owning_offer else ""
    if not has_professional_access(owning_phone):
        return jsonify({"success": False, "error": "One-click DocuSign send is a Professional-plan feature. Upgrade at txtanoffer.com/pricing."}), 403

    pdf_path = os.path.join(OUTPUT_DIR, pdf_filename)
    if not os.path.exists(pdf_path):
        return jsonify({"success": False, "error": "PDF not found"}), 404

    # Same required-fields gate as "Email to Listing Agent" -- a contract
    # missing a required field (e.g. buyer/seller legal name) must never
    # go out for e-signature either. The review page disables the button
    # when this fails, but that's only a UI convenience -- this endpoint
    # can be hit directly, so re-check server-side.
    if owning_offer and owning_offer.get("price"):
        down_amt = int(owning_offer["price"] * owning_offer["down_pct"])
        validation_parsed = {
            "address": owning_offer.get("address") or parsed.get("address", ""),
            "price": owning_offer["price"], "down_payment_amount": down_amt,
            "loan_amount": owning_offer["price"] - down_amt, "close_days": owning_offer["close_days"],
            "created_at": owning_offer.get("created_at"),
            "financing_type_specified": bool(owning_offer.get("financing_type")),
        }
    else:
        validation_parsed = parsed
    validation = validate_offer_pdf(pdf_path, validation_parsed)
    # Buyer/seller legal name is only a WARNING for Email/Download -- the
    # agent can still open the PDF and type the name in by hand before
    # emailing or printing it. That "fill in by hand" escape hatch doesn't
    # exist for DocuSign: this button routes straight to e-signature, so a
    # blank legal name must block here too, even though it doesn't block
    # the other two send paths.
    docusign_blocking = list(validation["blocking"]) + [
        w for w in validation["warnings"] if "legal name is blank" in w
    ]
    if docusign_blocking:
        return jsonify({
            "success": False,
            "error": "This contract is missing required fields and can't be sent for signature: " + "; ".join(docusign_blocking),
            "missing_fields": docusign_blocking,
        }), 422

    result = send_to_docusign(pdf_path, parsed, signer_email, signer_name)
    track_event("docusign_sent" if result["success"] else "docusign_failed", signer_email, result)
    return jsonify(result), 200 if result["success"] else 500


@app.route("/pricing")
def pricing():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pricing — TxtAnOffer</title>
<meta name="description" content="TxtAnOffer pricing plans for Texas real estate agents. Generate TREC contracts instantly from $39/month.">
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
  :root {
    --bg: #F5F5F7;
    --bg-card: #fff;
    --border: rgba(15,31,47,0.08);
    --border-hover: rgba(23,23,23,0.35);
    --text: #0f1f2f;
    --text-muted: #5a6b7a;
    --text-dim: #8a9aa9;
    --accent: #171717;
    --accent-light: #525252;
    --accent-dark: #000000;
    --accent-tint: #F0F0EE;
    --radius: 1.25rem;
    --radius-sm: 0.85rem;
    --transition: all 0.2s ease;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
    background:var(--bg);
    color:var(--text);
    line-height:1.5;
    -webkit-font-smoothing:antialiased;
    min-height:100vh;
  }
  a { color:inherit; text-decoration:none; }

  /* Nav */
  .nav {
    display:flex;align-items:center;justify-content:space-between;
    padding:1rem 2rem;position:sticky;top:0;
    background:rgba(255,255,255,0.85);backdrop-filter:blur(20px);
    -webkit-backdrop-filter:blur(20px);
    border-bottom:1px solid var(--border);z-index:100;
  }
  .nav-left {display:flex;align-items:center;gap:0.6rem;font-weight:700;font-size:1.1rem;letter-spacing:-0.02em;color:var(--text);}
  .nav-logo {width:34px;height:34px;border-radius:22%;overflow:hidden;}
  .nav-logo img {width:100%;height:100%;object-fit:contain;}
  .nav-links {display:flex;gap:2rem;font-size:0.875rem;font-weight:500;color:var(--text-muted);}
  .nav-links a {transition:var(--transition);}
  .nav-links a:hover {color:var(--text);}
  .nav-cta {
    background:var(--accent);color:#fff;padding:0.55rem 1.35rem;border-radius:9999px;
    font-size:0.875rem;font-weight:600;text-decoration:none;display:inline-block;
    transition:var(--transition);
  }
  .nav-cta:hover {transform:scale(1.05);box-shadow:0 0 24px rgba(23,23,23,0.3);}
  .nav-toggle { display: none; flex-direction: column; justify-content: center; gap: 5px; width: 34px; height: 34px; background: none; border: none; cursor: pointer; padding: 0; }
  .nav-toggle span { display: block; width: 100%; height: 2px; background: var(--text); border-radius: 2px; }

  /* Header */
  .page-header {text-align:center;padding:4rem 2rem 3rem;max-width:700px;margin:0 auto;}
  .page-header h1 {font-size:2.75rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.75rem;color:var(--text);}
  .page-header h1 .gradient {
    background:linear-gradient(135deg,var(--accent-light),var(--accent));
    -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
  }
  .page-header p {color:var(--text-muted);font-size:1.1rem;line-height:1.6;}

  /* Pricing Grid */
  .pricing-grid {
    display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:1.25rem;
    max-width:1100px;margin:0 auto;padding:0 2rem 3rem;
  }
  .pricing-card {
    background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
    padding:2rem 1.75rem;display:flex;flex-direction:column;transition:var(--transition);
    box-shadow:0 1px 3px rgba(15,31,47,0.05);
  }
  .pricing-card:hover {transform:translateY(-4px);border-color:var(--border-hover);box-shadow:0 12px 30px rgba(15,31,47,0.1);}
  .pricing-card.featured {border-color:var(--accent);position:relative;}
  .featured-badge {
    position:absolute;top:-0.75rem;left:50%;transform:translateX(-50%);
    font-size:0.65rem;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;
    color:var(--accent-dark);background:var(--bg);
    border:1px solid var(--accent);padding:0.25rem 0.75rem;border-radius:9999px;white-space:nowrap;
  }

  .plan-name {font-size:1.25rem;font-weight:700;color:var(--text);margin-bottom:0.25rem;}
  .plan-desc {font-size:0.85rem;color:var(--text-dim);margin-bottom:1.25rem;line-height:1.4;}
  .price-row {display:flex;align-items:baseline;gap:0.25rem;margin-bottom:1.25rem;}
  .price-current {font-size:2.5rem;font-weight:800;color:var(--text);}
  .price-period {font-size:0.9rem;color:var(--text-dim);}

  .features {list-style:none;margin:0 0 1.5rem;flex:1;}
  .features li {
    padding:0.5rem 0;font-size:0.85rem;color:var(--text-muted);
    display:flex;align-items:start;gap:0.5rem;
  }
  .check {color:var(--accent-dark);font-weight:700;font-size:0.9rem;}

  .cta-btn {
    display:block;width:100%;padding:0.85rem;
    background:linear-gradient(135deg,var(--accent),#000000);color:#fff;
    border:none;font-family:inherit;font-size:0.9rem;font-weight:600;
    border-radius:var(--radius-sm);cursor:pointer;text-align:center;
    transition:var(--transition);text-decoration:none;
  }
  .cta-btn:hover {transform:translateY(-2px);box-shadow:0 8px 24px rgba(23,23,23,0.25);}
  .cta-btn.outline {
    background:transparent;border:1px solid var(--border);color:var(--text-muted);
  }
  .cta-btn.outline:hover {border-color:var(--accent);color:var(--accent-dark);transform:translateY(-2px);}

  /* Value Props */
  .value-section {max-width:1100px;margin:0 auto;padding:3rem 2rem;border-top:1px solid var(--border);}
  .value-grid {display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:1.25rem;}
  .value-card {
    background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
    padding:1.75rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);
  }
  .value-title {
    font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;
    color:var(--accent-dark);margin-bottom:0.5rem;
  }
  .value-text {color:var(--text-muted);font-size:0.9rem;line-height:1.6;}

  /* Footer */
  .footer-note {text-align:center;padding:2rem;font-size:0.8rem;color:var(--text-dim);}
  .footer-note a {color:var(--accent-dark);}
  .footer-note a:hover {text-decoration:underline;}

  @media(max-width:600px) {
    .page-header h1 {font-size:2rem;}
    .pricing-grid {padding:0 1rem 2rem;}
    .nav-toggle { display: flex; }
    .nav-links {
      display: none; position: absolute; top: 100%; left: 0; right: 0;
      flex-direction: column; gap: 0; padding: 0.5rem 1.25rem 1.25rem;
      background: #fff; border-bottom: 1px solid rgba(15,31,47,0.08);
    }
    .nav-links.open { display: flex; }
    .nav-links a { padding: 0.75rem 0; border-bottom: 1px solid rgba(15,31,47,0.08); }
    .nav-links a:last-child { border-bottom: none; }
  }
</style>
</head>
<body>

<nav class="nav">
  <a href="/" class="nav-left">
    <div class="nav-logo"><img src="/static/logo.svg" alt="TxtAnOffer"></div>
    <span>TxtAnOffer</span>
  </a>
  <div class="nav-links" id="navLinks">
    <a href="/#how">How it works</a>
    <a href="/#trust">Accuracy</a>
    <a href="/pricing">Pricing</a>
    <a href="/demo">Demo</a>
    <a href="/playground">Parser Playground</a>
    <a href="/faq">FAQ</a>
    <a href="/about">About</a>
    <a href="/contact">Support</a>
    <a href="/login">Log In</a>
  </div>
  <a href="/signup" class="nav-cta">Start Free Trial</a>
  <button class="nav-toggle" id="navToggle" aria-label="Menu" aria-expanded="false"><span></span><span></span><span></span></button>
</nav>
<script>
(function(){
  var t=document.getElementById('navToggle'), l=document.getElementById('navLinks');
  if(!t||!l) return;
  t.addEventListener('click', function(){
    var open = l.classList.toggle('open');
    t.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  l.querySelectorAll('a').forEach(function(a){
    a.addEventListener('click', function(){ l.classList.remove('open'); t.setAttribute('aria-expanded','false'); });
  });
})();
</script>

<div class="page-header">
  <h1>Simple pricing.<br><span class="gradient">Massive time savings.</span></h1>
  <p>Stop spending 45 minutes per offer. Pick a plan and start generating contracts in seconds.</p>
  <p style="margin-top:1rem;color:var(--accent-dark);font-weight:600;font-size:0.95rem;">Try free — 3 offers, no card required.</p>
</div>

<div class="pricing-grid">

  <div class="pricing-card">
    <h2 class="plan-name">Starter</h2>
    <p class="plan-desc">Submit offers faster than any other agent in the room.</p>
    <div class="price-row">
      <span class="price-current">$39</span>
      <span class="price-period">/month</span>
    </div>
    <ul class="features">
      <li><span class="check">&#10003;</span> Unlimited offers via SMS or web</li>
      <li><span class="check">&#10003;</span> TREC 20-19 + Financing Addendum</li>
      <li><span class="check">&#10003;</span> Contract amendments (TREC 39-11)</li>
      <li><span class="check">&#10003;</span> 10-second contract generation</li>
      <li><span class="check">&#10003;</span> Agent profile auto-fill</li>
      <li><span class="check">&#10003;</span> Email delivery to listing agents</li>
      <li><span class="check">&#10003;</span> Offer history dashboard</li>
    </ul>
    <form action="/create-checkout-session" method="POST">
      <input type="hidden" name="plan" value="starter">
      <button type="submit" class="cta-btn">Start Free Trial</button>
    </form>
    <p style="text-align:center;font-size:0.75rem;color:var(--text-dim);margin-top:0.75rem;">3 free offers, then $39/mo. Cancel anytime.</p>
  </div>

  <div class="pricing-card featured">
    <span class="featured-badge">Most Popular</span>
    <h2 class="plan-name">Professional</h2>
    <p class="plan-desc">Close deals faster with one-click signing and CRM automation.</p>
    <div class="price-row">
      <span class="price-current">$79</span>
      <span class="price-period">/month</span>
    </div>
    <ul class="features">
      <li><span class="check">&#10003;</span> Everything in Starter</li>
      <li><span class="check">&#10003;</span> One-click DocuSign send</li>
      <li><span class="check">&#10003;</span> Webhook automation (Zapier-compatible)</li>
      <li><span class="check">&#10003;</span> Agent branding on offer pages</li>
      <li><span class="check">&#10003;</span> Priority support</li>
    </ul>
    <form action="/create-checkout-session" method="POST">
      <input type="hidden" name="plan" value="professional">
      <button type="submit" class="cta-btn">Start Free Trial</button>
    </form>
    <p style="text-align:center;font-size:0.75rem;color:var(--text-dim);margin-top:0.75rem;">3 free offers, then $79/mo. Cancel anytime.</p>
  </div>

  <div class="pricing-card">
    <h2 class="plan-name">Enterprise</h2>
    <p class="plan-desc">For large brokerages and franchises.</p>
    <div class="price-row">
      <span class="price-current">Custom</span>
    </div>
    <ul class="features">
      <li><span class="check">&#10003;</span> Everything in Professional</li>
      <li><span class="check">&#10003;</span> Dedicated onboarding call</li>
      <li><span class="check">&#10003;</span> SLA &amp; dedicated support</li>
    </ul>
    <a href="mailto:hello@txtanoffer.com?subject=Enterprise%20Plan" class="cta-btn outline">Contact Us</a>
  </div>

</div>

<div class="value-section">
  <div class="value-grid">
    <div class="value-card">
      <div class="value-title">Time ROI</div>
      <div class="value-text">Save 45 minutes per offer. At 5 offers/month, that's 3.75 hours back &mdash; worth $187-$562 of your time.</div>
    </div>
    <div class="value-card">
      <div class="value-title">Zero Errors</div>
      <div class="value-text">Math calculated automatically. No more "$21,750 or 3%?" double-checking. Every field consistent.</div>
    </div>
    <div class="value-card">
      <div class="value-title">Cheaper Than Coffee</div>
      <div class="value-text">At 5 offers/month, Starter costs $7.80 per contract. Less than a coffee for 45 minutes of your time back.</div>
    </div>
  </div>
</div>

<div style="max-width:640px;margin:0 auto;padding:2rem;text-align:center;">
  <div style="background:var(--accent-tint);border:1px solid rgba(23,23,23,0.2);border-radius:1rem;padding:2rem 1.75rem;">
    <div style="font-size:1.5rem;margin-bottom:0.5rem;">&#128737;</div>
    <h3 style="font-size:1.1rem;font-weight:700;margin-bottom:0.5rem;">Zero-Risk Guarantee</h3>
    <p style="color:var(--text-muted);font-size:0.9rem;line-height:1.7;margin:0;">
      Start with <strong style="color:var(--text);">3 free offers</strong> — no credit card required.
      When you subscribe, cancel anytime from your dashboard — no contracts, no fees, no questions asked.
      Cancellation takes effect at the end of your billing cycle so you keep access through the period you paid for.
    </p>
  </div>
</div>

<div class="footer-note">
  All plans cancel anytime. No contracts. By subscribing you agree to our <a href="/terms">Terms of Service</a>.
  <br><br>
  <a href="/demo">&larr; Try the demo</a> &middot; <a href="/">Home</a>
</div>

</body>
</html>
"""


@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    """Create Stripe checkout session for subscription"""
    plan = request.form.get("plan", "starter")
    if plan not in ("starter", "professional", "brokerage"):
        plan = "starter"
    price_map = {
        "starter": STRIPE_PRICE_ID,
        "professional": STRIPE_PRICE_ID_PRO,
        "brokerage": STRIPE_PRICE_ID_BROKERAGE,
    }
    price_id = price_map.get(plan, STRIPE_PRICE_ID)

    if not stripe.api_key or not price_id:
        return redirect("mailto:hello@txtanoffer.com?subject=Early%20Adopter%20Signup")

    try:
        checkout_session = stripe.checkout.Session.create(
            line_items=[{
                'price': price_id,
                'quantity': 1,
            }],
            mode='subscription',
            phone_number_collection={'enabled': True},
            success_url=request.host_url + 'success?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=request.host_url + 'pricing',
            allow_promotion_codes=True,
            metadata={'plan': plan},
        )
        return redirect(checkout_session.url, code=303)
    except Exception as e:
        return jsonify(error=str(e)), 400


@app.route("/success")
def success():
    """Payment success page"""
    session_id = request.args.get('session_id')
    # Try to get phone from the checkout session to pre-fill profile
    phone_from_checkout = ""
    if session_id and stripe.api_key:
        try:
            sess = stripe.checkout.Session.retrieve(session_id)
            phone_from_checkout = sess.customer_details.get('phone', '') if sess.customer_details else ''
        except Exception:
            pass
    profile_link = sign_dashboard_url(phone_from_checkout, request.host_url.rstrip("/")).replace("/dashboard?", "/profile?")
    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Welcome to TxtAnOffer!</title>
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
  :root{{--bg:#F5F5F7;--bg-card:#fff;--border:rgba(15,31,47,0.08);
    --text:#0f1f2f;--text-muted:#5a6b7a;--text-dim:#8a9aa9;
    --accent:#171717;--accent-light:#525252;--accent-dark:#000000;--accent-tint:#F0F0EE;
    --radius:1.25rem;--radius-sm:0.85rem;}}
  *{{margin:0;padding:0;box-sizing:border-box;}}
  body{{background:var(--bg);min-height:100vh;margin:0;display:flex;align-items:center;
    justify-content:center;padding:2rem;font-family:'Inter',-apple-system,sans-serif;color:var(--text);}}
  .card{{background:var(--bg-card);border:1px solid var(--border);padding:3rem;border-radius:var(--radius);
    max-width:520px;width:100%;text-align:center;box-shadow:0 1px 3px rgba(15,31,47,0.05);}}
  h1{{font-size:2rem;font-weight:800;margin:0 0 0.75rem;letter-spacing:-0.02em;color:var(--text);}}
  .sub{{color:var(--text-muted);font-size:1rem;line-height:1.6;margin-bottom:1.5rem;}}
  .next-steps{{text-align:left;background:var(--accent-tint);border:1px solid var(--border);
    padding:1.5rem;border-radius:var(--radius-sm);margin-bottom:1.5rem;}}
  .next-steps h3{{font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;
    color:var(--accent-dark);margin:0 0 0.75rem;}}
  .next-steps ol{{margin:0;padding-left:1.25rem;}}
  .next-steps li{{margin:0.5rem 0;font-size:0.9rem;color:var(--text-muted);line-height:1.5;}}
  .next-steps li strong{{color:var(--text);}}
  .btn{{display:inline-block;padding:0.85rem 2rem;
    background:linear-gradient(135deg,var(--accent),#000000);color:#fff;
    text-decoration:none;border-radius:var(--radius-sm);font-weight:600;font-size:0.95rem;
    transition:all 0.2s ease;}}
  .btn:hover{{transform:translateY(-2px);box-shadow:0 8px 24px rgba(23,23,23,0.25);}}
  .logo{{margin-bottom:1.5rem;}}
  .logo img{{width:48px;height:48px;border-radius:22%;object-fit:contain;}}
</style>
</head>
<body>
  <div class="card">
    <div class="logo"><a href="/"><img src="/static/logo.svg" alt="TxtAnOffer"></a></div>
    <h1>Welcome aboard!</h1>
    <p class="sub">Your subscription is active. You're all set with <strong>unlimited offers</strong>.</p>

    <div class="next-steps">
      <h3>Next Steps</h3>
      <ol>
        <li><strong>Set up your profile</strong> &mdash; your name, license, and brokerage auto-fill every offer</li>
        <li>Text your first offer to <strong>1-833-897-0333</strong></li>
        <li>Or use the web demo at <strong>txtanoffer.com/demo</strong></li>
      </ol>
    </div>

    <a href="{profile_link}" class="btn">Set Up Your Profile &rarr;</a>
  </div>
</body>
</html>
"""


@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    """Handle Stripe webhooks for subscription events"""
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')
    webhook_secret = os.environ.get('STRIPE_WEBHOOK_SECRET', '')

    if not webhook_secret:
        return jsonify(error="Webhook secret not configured"), 503

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, webhook_secret
        )
    except ValueError:
        return jsonify(error='Invalid payload'), 400
    except stripe.error.SignatureVerificationError:
        return jsonify(error='Invalid signature'), 400

    # Handle subscription events
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        customer_email = session['customer_details']['email']
        customer_phone = session['customer_details'].get('phone', '')
        customer_id = session['customer']
        subscription_id = session['subscription']
        plan = (session.get('metadata') or {}).get('plan', 'starter')

        # Activate subscription on agent's phone number
        if customer_phone:
            user = get_user(customer_phone)
            if not user:
                create_user(customer_phone)
            activate_subscription(customer_phone, customer_id, subscription_id, plan=plan)

        # Track conversion
        track_event("subscription_created", customer_phone, metadata={
            "customer_id": customer_id,
            "email": customer_email
        })

    elif event['type'] == 'customer.subscription.deleted':
        subscription = event['data']['object']
        deactivate_subscription(subscription['id'])
        track_event("subscription_canceled", metadata={
            "subscription_id": subscription['id']
        })

    return jsonify(success=True)


@app.route("/analytics")
def analytics_dashboard():
    if not ANALYTICS_PASSWORD:
        abort(503)
    token = request.args.get("token", "")
    if not hmac.compare_digest(token, ANALYTICS_PASSWORD):
        abort(403)

    metrics = get_conversion_metrics(days=30)
    revenue = get_revenue_metrics()
    recent_sms = get_recent_sms(limit=20)
    recent_failures = get_recent_sms_failures(limit=20)
    waitlist_signups = get_waitlist_signups(limit=200)
    signups_by_source = get_signups_by_source(days=30)
    landing_visits_by_source = get_landing_visits_by_source(days=30)
    tc_check_summary = get_tc_check_summary(days=30)

    sms_rows = ""
    for sms in recent_sms:
        # Format timestamp
        from datetime import datetime
        dt = datetime.fromisoformat(sms['created_at'])
        time_str = dt.strftime("%m/%d %H:%M")
        sms_rows += f"<tr><td>{time_str}</td><td>{sms['phone']}</td><td>{sms['body'][:50]}</td></tr>"

    failure_rows = ""
    for fail in recent_failures:
        from datetime import datetime
        dt = datetime.fromisoformat(fail['created_at'])
        time_str = dt.strftime("%m/%d %H:%M")
        failure_rows += (
            f"<tr><td>{time_str}</td><td>{fail['phone']}</td>"
            f"<td>{fail['error'][:80]}</td><td>{fail['body']}</td></tr>"
        )

    waitlist_by_state = {}
    for w in waitlist_signups:
        waitlist_by_state[w["state"]] = waitlist_by_state.get(w["state"], 0) + 1
    waitlist_summary_rows = "".join(
        f"<tr><td>{state}</td><td>{count}</td></tr>"
        for state, count in sorted(waitlist_by_state.items(), key=lambda kv: -kv[1])
    ) or '<tr><td colspan="2" style="padding:10px;color:#666;">No waitlist signups yet.</td></tr>'
    source_rows = "".join(
        f"<tr><td>{s['source']}</td><td>{s['count']}</td></tr>" for s in signups_by_source
    ) or '<tr><td colspan="2" style="padding:10px;color:#666;">No signups yet.</td></tr>'
    visit_rows = "".join(
        f"<tr><td>{v['source']}</td><td>{v['count']}</td></tr>" for v in landing_visits_by_source
    ) or '<tr><td colspan="2" style="padding:10px;color:#666;">No tagged visits yet.</td></tr>'
    tc_issue_rows = "".join(
        f"<tr><td>{i['label']}</td><td>{i['count']}</td><td>{i['pct_of_recognized']}%</td></tr>"
        for i in tc_check_summary['issue_frequency']
    ) or '<tr><td colspan="3" style="padding:10px;color:#666;">No checks recognized yet.</td></tr>'
    waitlist_rows = ""
    for w in waitlist_signups[:20]:
        from datetime import datetime
        dt = datetime.fromisoformat(w['created_at'])
        time_str = dt.strftime("%m/%d %H:%M")
        waitlist_rows += f"<tr><td>{time_str}</td><td>{w['phone']}</td><td>{w['state']}</td></tr>"

    return f"""
<!DOCTYPE html>
<html><head><title>TxtAnOffer Analytics</title>
<style>
body{{font-family:system-ui;max-width:800px;margin:40px auto;padding:20px;}}
.metric{{background:#f5f5f5;padding:20px;margin:10px 0;border-radius:8px;}}
.metric h3{{margin:0 0 10px;color:#333;}}
.metric .value{{font-size:32px;font-weight:bold;color:#A9772F;}}
.metric .label{{color:#666;font-size:14px;}}
</style></head><body>
<h1>TxtAnOffer Analytics</h1>
<h2>Last 30 Days</h2>
<div class="metric">
  <h3>Conversion Funnel</h3>
  <div class="value">{metrics['overall_conversion_rate']}%</div>
  <div class="label">Free → Paid Conversion Rate</div>
  <p>{metrics['signups']} signups → {metrics['conversions']} paid</p>
</div>
<div class="metric">
  <h3>Signups by Source (30 days)</h3>
  <table style="width:100%;border-collapse:collapse;margin-top:10px;">
    <tr style="background:#eee;text-align:left;">
      <th style="padding:8px;">Source</th>
      <th style="padding:8px;">Signups</th>
    </tr>
    {source_rows}
  </table>
  <p class="label" style="margin-top:8px;">Tag outreach links with <code>?src=name</code> (e.g. <code>txtanoffer.com/signup?src=direct_reach</code>) to attribute signups here.</p>
</div>
<div class="metric">
  <h3>Landing Page Visits by Source (30 days)</h3>
  <table style="width:100%;border-collapse:collapse;margin-top:10px;">
    <tr style="background:#eee;text-align:left;">
      <th style="padding:8px;">Source</th>
      <th style="padding:8px;">Visits</th>
    </tr>
    {visit_rows}
  </table>
  <p class="label" style="margin-top:8px;">Raw clicks on a <code>?src=</code> link, counted even if the visitor never signs up &mdash; tells you whether a channel is being opened at all vs. opened-but-not-converting.</p>
</div>
<div class="metric">
  <h3>Trial Activation</h3>
  <div class="value">{metrics['trial_activation_rate']}%</div>
  <div class="label">Users who complete 3 free offers</div>
  <p>{metrics['trial_completions']} / {metrics['signups']} users</p>
</div>
<div class="metric">
  <h3>Paywall → Paid</h3>
  <div class="value">{metrics['paywall_to_paid_rate']}%</div>
  <div class="label">Users who pay after hitting limit</div>
  <p>{metrics['conversions']} / {metrics['hit_paywall']} users</p>
</div>
<div class="metric">
  <h3>Usage</h3>
  <div class="value">{metrics['total_offers']}</div>
  <div class="label">Total offers generated</div>
  <p>{metrics['avg_offers_per_user']} offers per user average</p>
</div>
<div class="metric">
  <h3>TC File Check</h3>
  <div class="value">{tc_check_summary['total']}</div>
  <div class="label">Files checked via /tc-check (30 days)</div>
  <p>{tc_check_summary['recognized']} recognized as a TREC 20-19 &middot; {tc_check_summary['complete']} came back complete ({tc_check_summary['completion_rate']}%)</p>
</div>
<div class="metric">
  <h3>TC File Check &rarr; Email Capture</h3>
  <div class="value">{tc_check_summary['gate_conversion_rate']}%</div>
  <div class="label">Of uploads that hit the itemized-report gate, gave an email</div>
  <p>{tc_check_summary['emails_captured']} emails / {tc_check_summary['gated']} gated uploads</p>
</div>
<div class="metric">
  <h3>TC File Check &mdash; Top Issues (30 days)</h3>
  <table style="width:100%;border-collapse:collapse;margin-top:10px;">
    <tr style="background:#eee;text-align:left;">
      <th style="padding:8px;">Issue</th>
      <th style="padding:8px;">Count</th>
      <th style="padding:8px;">% of recognized files</th>
    </tr>
    {tc_issue_rows}
  </table>
  <p class="label" style="margin-top:8px;">Whatever's at the top of this list is both the next marketing hook ("audited N files, X% are missing...") and a candidate for a dedicated feature or reminder.</p>
</div>
<h2>Revenue</h2>
<div class="metric">
  <h3>Active Subscribers</h3>
  <div class="value">{revenue['active_subscribers']}</div>
  <div class="label">Paying customers</div>
</div>
<div class="metric">
  <h3>MRR</h3>
  <div class="value">${revenue['mrr']:,}</div>
  <div class="label">Monthly Recurring Revenue</div>
</div>
<div class="metric">
  <h3>ARR</h3>
  <div class="value">${revenue['arr']:,}</div>
  <div class="label">Annual Recurring Revenue</div>
</div>
<h2>Recent SMS Activity</h2>
<table style="width:100%;border-collapse:collapse;">
  <tr style="background:#f5f5f5;text-align:left;">
    <th style="padding:10px;">Time</th>
    <th style="padding:10px;">Phone</th>
    <th style="padding:10px;">Message</th>
  </tr>
  {sms_rows}
</table>
<h2 style="color:{'#c0392b' if failure_rows else '#333'};">Recent Send Failures{' &#9888;' if failure_rows else ''}</h2>
<table style="width:100%;border-collapse:collapse;">
  <tr style="background:#f5f5f5;text-align:left;">
    <th style="padding:10px;">Time</th>
    <th style="padding:10px;">Phone</th>
    <th style="padding:10px;">Error</th>
    <th style="padding:10px;">Message</th>
  </tr>
  {failure_rows or '<tr><td colspan="4" style="padding:10px;color:#666;">None &mdash; outbound sends are working.</td></tr>'}
</table>
<h2>Out-of-State Waitlist ({len(waitlist_signups)} total)</h2>
<table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
  <tr style="background:#f5f5f5;text-align:left;">
    <th style="padding:10px;">State</th>
    <th style="padding:10px;">Signups</th>
  </tr>
  {waitlist_summary_rows}
</table>
<table style="width:100%;border-collapse:collapse;">
  <tr style="background:#f5f5f5;text-align:left;">
    <th style="padding:10px;">Time</th>
    <th style="padding:10px;">Phone</th>
    <th style="padding:10px;">State</th>
  </tr>
  {waitlist_rows or '<tr><td colspan="3" style="padding:10px;color:#666;">None yet.</td></tr>'}
</table>
<p style="color:#666;font-size:12px;margin-top:20px;">
  Check Twilio console for full logs: <a href="https://console.twilio.com/" target="_blank">console.twilio.com</a>
</p>
</body></html>
"""


@app.route("/signup", methods=["GET", "POST"])
def signup():
    success_msg = ""
    # Attribution: ?src=direct_reach on the link (GET) is carried through the
    # form as a hidden field so the POST can record which channel drove the
    # signup -- see get_signups_by_source() on /analytics. Falls back to the
    # ta_src first-touch cookie (set on homepage landing) for signups that
    # happen on a later visit/page with no ?src on the actual signup click.
    import re as _re
    src = _re.sub(r"[^a-zA-Z0-9_-]", "", request.values.get("src", "") or request.cookies.get("ta_src", ""))[:60]
    if request.method == "POST":
        phone = request.form.get("phone", "")
        name = request.form.get("name", "")
        email = request.form.get("email", "")
        if phone:
            account_ok = True
            try:
                if not get_user(phone):
                    create_user(phone)
            except Exception as e:
                account_ok = False
                print(f"[SIGNUP] create_user failed for {phone}: {e}")

            if not account_ok:
                success_msg = (
                    '<div class="error">Something went wrong creating your account. '
                    'Please try again, or text your offer directly to (833) 897-0333 to get started.</div>'
                )
            else:
                try:
                    track_event("signup", phone, {"name": name, "email": email, "source": src or "direct"})
                except Exception:
                    pass
                sms_sent = twilio_send_sms(phone,
                    "Welcome to TxtAnOffer! Text your offer: 725k 3% 21day 123 Main St. "
                    "Msg & data rates may apply. Reply STOP to opt out."
                )
                profile_url = sign_dashboard_url(phone, request.host_url.rstrip("/")).replace("/dashboard?", "/profile?")
                intro_line = "Check your texts for a welcome message." if sms_sent else "Tap below to set up your profile now."
                success_msg = (
                    '<div class="success">'
                    f'<strong>You\'re in!</strong> {intro_line}<br><br>'
                    '<span style="font-size:0.8rem;color:var(--text-muted);">You have 3 free offers to try it out.</span>'
                    '</div>'
                    '<div style="display:flex;gap:0.5rem;margin-top:1rem;flex-wrap:wrap;">'
                    f'<a href="{profile_url}" style="flex:1;text-align:center;padding:0.75rem 1rem;'
                    'background:linear-gradient(135deg,var(--accent),#000000);color:#fff;border-radius:var(--radius-sm);'
                    'font-weight:600;font-size:0.85rem;text-decoration:none;">Set Up Your Profile &rarr;</a>'
                    '<a href="/pricing" style="flex:1;text-align:center;padding:0.75rem 1rem;'
                    'background:var(--bg-card);color:var(--text-muted);border:1px solid var(--border);'
                    'border-radius:var(--radius-sm);font-weight:600;font-size:0.85rem;text-decoration:none;">View Plans</a>'
                    '</div>'
                )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sign Up — TxtAnOffer</title>
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
  :root{{--bg:#F5F5F7;--bg-card:#fff;--border:rgba(15,31,47,0.08);
    --text:#0f1f2f;--text-muted:#5a6b7a;--text-dim:#8a9aa9;
    --accent:#171717;--accent-light:#525252;--accent-dark:#000000;--accent-tint:#F0F0EE;
    --radius:1.25rem;--radius-sm:0.85rem;--transition:all 0.2s ease;}}
  *{{margin:0;padding:0;box-sizing:border-box;}}
  body{{background:var(--bg);min-height:100vh;margin:0;display:flex;align-items:center;
    justify-content:center;padding:2rem;font-family:'Inter',-apple-system,sans-serif;color:var(--text);}}
  a{{color:inherit;text-decoration:none;}}
  .wrap{{width:100%;max-width:460px;}}
  .nav-back{{display:flex;align-items:center;gap:0.5rem;margin-bottom:1.5rem;}}
  .nav-back img{{width:28px;height:28px;border-radius:22%;object-fit:contain;}}
  .nav-back span{{font-size:0.85rem;color:var(--text-muted);}}
  .nav-back:hover span{{color:var(--text);}}
  h1{{font-size:1.75rem;font-weight:800;letter-spacing:-0.02em;margin-bottom:0.5rem;color:var(--text);}}
  .sub{{color:var(--text-muted);font-size:0.95rem;line-height:1.6;margin-bottom:1.5rem;}}
  .card{{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:1.75rem;
    box-shadow:0 1px 3px rgba(15,31,47,0.05);}}
  .field-label{{font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;
    color:var(--text-dim);margin-bottom:0.4rem;display:block;}}
  input[type=text],input[type=tel],input[type=email]{{
    width:100%;background:#fff;border:1px solid rgba(15,31,47,0.14);
    border-radius:var(--radius-sm);padding:0.75rem 1rem;color:var(--text);
    font-size:0.95rem;font-family:inherit;outline:none;margin-bottom:1rem;transition:var(--transition);
  }}
  input:focus{{border-color:var(--accent);box-shadow:0 0 0 3px rgba(23,23,23,0.15);}}
  input::placeholder{{color:#b8c2ca;}}
  .consent-row{{
    display:flex;align-items:flex-start;gap:0.75rem;margin:1rem 0;padding:1rem;
    background:var(--accent-tint);border:1px solid rgba(23,23,23,0.2);border-radius:var(--radius-sm);
  }}
  .consent-row input[type=checkbox]{{margin-top:0.2rem;width:18px;height:18px;flex-shrink:0;accent-color:var(--accent);}}
  .consent-row label{{font-size:0.8rem;line-height:1.6;color:var(--text-muted);}}
  .consent-row a{{color:var(--accent-dark);text-decoration:underline;}}
  button{{
    width:100%;margin-top:0.75rem;
    background:linear-gradient(135deg,var(--accent),#000000);color:#fff;border:none;
    padding:0.85rem;font-family:inherit;font-size:0.95rem;font-weight:600;
    border-radius:var(--radius-sm);cursor:pointer;transition:var(--transition);
  }}
  button:hover{{transform:translateY(-2px);box-shadow:0 8px 24px rgba(23,23,23,0.25);}}
  button:disabled{{opacity:0.4;cursor:not-allowed;transform:none;box-shadow:none;}}
  .success{{
    margin-top:1rem;padding:1rem;background:var(--accent-tint);
    border:1px solid rgba(23,23,23,0.2);border-radius:var(--radius-sm);
    font-size:0.9rem;color:var(--accent-dark);text-align:center;
  }}
  .error{{
    margin-top:1rem;padding:1rem;background:rgba(239,68,68,0.08);
    border:1px solid rgba(239,68,68,0.2);border-radius:var(--radius-sm);
    font-size:0.9rem;color:#dc2626;text-align:center;
  }}
  .foot{{text-align:center;margin-top:1.5rem;font-size:0.8rem;color:var(--text-dim);}}
  .foot a{{color:var(--accent-dark);text-decoration:none;}}
  .foot a:hover{{text-decoration:underline;}}
</style>
</head>
<body>
  <div class="wrap">
    <a href="/" class="nav-back"><img src="/static/logo.svg" alt=""><span>&larr; TxtAnOffer</span></a>
    <h1>Get started with TxtAnOffer</h1>
    <p class="sub">Enter your phone number to receive offer drafts via SMS at +1 (833) 897-0333.</p>
    <div class="card">
      <form method="POST" action="/signup" id="signup-form">
        <input type="hidden" name="src" value="{src}">
        <label class="field-label">Phone number</label>
        <input type="tel" name="phone" placeholder="+1 (555) 123-4567" required>
        <label class="field-label">Name</label>
        <input type="text" name="name" placeholder="Your name">
        <label class="field-label">Email</label>
        <input type="email" name="email" placeholder="you@brokerage.com">
        <div class="consent-row">
          <input type="checkbox" id="sms-consent" name="sms_consent">
          <label for="sms-consent">(Optional) I agree to receive automated transactional SMS messages from TxtAnOffer at +1 (833) 897-0333 about my offer drafts. Message frequency varies based on usage. Reply STOP to opt out, HELP for help. Msg &amp; data rates may apply. Consent is not a condition of purchase or service. <a href="/privacy">Privacy Policy</a> &amp; <a href="/terms">Terms</a></label>
        </div>
        <button type="submit">Sign up for SMS</button>
      </form>
      {success_msg}
    </div>
    <div class="foot"><a href="/privacy">Privacy Policy</a> &middot; <a href="/terms">Terms</a> &middot; <a href="/demo">Try the demo</a></div>
  </div>
</body>
</html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    message = ""
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        # Normalize phone
        import re
        phone_clean = re.sub(r"[^\d+]", "", phone)
        if not phone_clean.startswith("+"):
            phone_clean = "+1" + phone_clean.lstrip("1")

        user = get_user(phone_clean)
        if user:
            # Send dashboard link via Twilio
            try:
                dash_link = sign_dashboard_url(phone_clean, request.host_url.rstrip("/"))
                if twilio_send_sms(phone_clean, f"Your dashboard:\n{dash_link}"):
                    message = "sent"
                else:
                    message = "error"
            except Exception as e:
                print(f"[LOGIN] SMS send failed: {e}")
                message = "error"
        else:
            message = "not_found"

    msg_html = ""
    if message == "sent":
        msg_html = '<div class="msg success">Check your texts! We sent a login link to your phone.</div>'
    elif message == "not_found":
        msg_html = '<div class="msg error">No account found for that number. <a href="/signup">Sign up first</a>.</div>'
    elif message == "error":
        msg_html = '<div class="msg error">Could not send SMS. Text DASHBOARD to (833) 897-0333 instead.</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Log In — TxtAnOffer</title>
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
  :root{{--bg:#F5F5F7;--bg-card:#fff;--border:rgba(15,31,47,0.08);
    --text:#0f1f2f;--text-muted:#5a6b7a;--text-dim:#8a9aa9;
    --accent:#171717;--accent-light:#525252;--accent-dark:#000000;--accent-tint:#F0F0EE;
    --radius:1.25rem;--radius-sm:0.85rem;--transition:all 0.2s ease;}}
  *{{margin:0;padding:0;box-sizing:border-box;}}
  body{{background:var(--bg);min-height:100vh;margin:0;display:flex;align-items:center;
    justify-content:center;padding:2rem;font-family:'Inter',-apple-system,sans-serif;color:var(--text);}}
  a{{color:inherit;text-decoration:none;}}
  .wrap{{width:100%;max-width:400px;}}
  .nav-back{{display:flex;align-items:center;gap:0.5rem;margin-bottom:1.5rem;}}
  .nav-back img{{width:28px;height:28px;border-radius:22%;object-fit:contain;}}
  .nav-back span{{font-size:0.85rem;color:var(--text-muted);}}
  .nav-back:hover span{{color:var(--text);}}
  h1{{font-size:1.75rem;font-weight:800;letter-spacing:-0.02em;margin-bottom:0.5rem;color:var(--text);}}
  .sub{{color:var(--text-muted);font-size:0.95rem;margin-bottom:1.5rem;line-height:1.5;}}
  .card{{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:1.75rem;
    box-shadow:0 1px 3px rgba(15,31,47,0.05);}}
  label{{font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;
    color:var(--text-dim);display:block;margin-bottom:0.4rem;}}
  input{{
    width:100%;background:#fff;border:1px solid rgba(15,31,47,0.14);
    border-radius:var(--radius-sm);padding:0.75rem 1rem;color:var(--text);
    font-size:0.95rem;font-family:inherit;outline:none;transition:var(--transition);
  }}
  input:focus{{border-color:var(--accent);box-shadow:0 0 0 3px rgba(23,23,23,0.15);}}
  input::placeholder{{color:#b8c2ca;}}
  .sms-note{{font-size:0.8rem;color:var(--text-dim);margin:0.75rem 0 0;line-height:1.5;}}
  button{{
    width:100%;margin-top:1rem;
    background:linear-gradient(135deg,var(--accent),#000000);color:#fff;border:none;
    padding:0.85rem;font-family:inherit;font-size:0.95rem;font-weight:600;
    border-radius:var(--radius-sm);cursor:pointer;transition:var(--transition);
  }}
  button:hover{{transform:translateY(-2px);box-shadow:0 8px 24px rgba(23,23,23,0.25);}}
  .msg{{margin-top:1rem;padding:0.85rem;border-radius:var(--radius-sm);font-size:0.9rem;text-align:center;}}
  .msg.success{{background:var(--accent-tint);border:1px solid rgba(23,23,23,0.2);color:var(--accent-dark);}}
  .msg.error{{background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.2);color:#dc2626;}}
  .msg a{{color:var(--accent-dark);}}
  .alt{{text-align:center;margin-top:1.25rem;font-size:0.85rem;color:var(--text-dim);}}
  .alt a{{color:var(--accent-dark);text-decoration:none;}}
  .alt a:hover{{text-decoration:underline;}}
</style>
</head>
<body>
<div class="wrap">
  <a href="/" class="nav-back"><img src="/static/logo.svg" alt=""><span>&larr; TxtAnOffer</span></a>
  <h1>Log In</h1>
  <p class="sub">Enter your phone number and we'll text you a link to your dashboard.</p>
  <div class="card">
    <form method="POST">
      <label>Phone number</label>
      <input type="tel" name="phone" placeholder="(512) 555-1234" required>
      <p class="sms-note">By clicking below, you agree to receive one SMS message from TxtAnOffer at +1 (833) 897-0333 containing your login link. Msg &amp; data rates may apply. Reply STOP to opt out.</p>
      <button type="submit">Send Login Link via SMS</button>
    </form>
    {msg_html}
  </div>
  <p class="alt">Don't have an account? <a href="/signup">Sign up</a></p>
</div>
</body>
</html>"""


@app.route("/terms")
def terms():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Terms of Service — TxtAnOffer</title>
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
  :root {
    --bg: #F5F5F7;
    --bg-card: #fff;
    --border: rgba(15,31,47,0.08);
    --border-hover: rgba(0,0,0,0.35);
    --text: #0f1f2f;
    --text-muted: #5a6b7a;
    --text-dim: #8a9aa9;
    --accent: #171717;
    --accent-light: #525252;
    --accent-dark: #000000;
    --accent-tint: #F0F0EE;
    --radius: 1.25rem;
    --radius-sm: 0.85rem;
    --transition: all 0.2s ease;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
    background:var(--bg);
    color:var(--text);
    line-height:1.5;
    -webkit-font-smoothing:antialiased;
    min-height:100vh;
  }
  a { color:inherit; text-decoration:none; }

  .nav {
    display:flex;align-items:center;justify-content:space-between;
    padding:1rem 2rem;position:sticky;top:0;
    background:rgba(255,255,255,0.85);backdrop-filter:blur(20px);
    -webkit-backdrop-filter:blur(20px);
    border-bottom:1px solid var(--border);z-index:100;
  }
  .nav-left {display:flex;align-items:center;gap:0.6rem;font-weight:700;font-size:1.1rem;letter-spacing:-0.02em;color:var(--text);}
  .nav-logo {width:34px;height:34px;border-radius:22%;overflow:hidden;}
  .nav-logo img {width:100%;height:100%;object-fit:contain;}
  .nav-links {display:flex;gap:2rem;font-size:0.875rem;font-weight:500;color:var(--text-muted);}
  .nav-links a {transition:var(--transition);}
  .nav-links a:hover {color:var(--text);}
  .nav-cta {
    background:var(--accent);color:#fff;padding:0.55rem 1.35rem;border-radius:9999px;
    font-size:0.875rem;font-weight:600;text-decoration:none;display:inline-block;
    transition:var(--transition);
  }
  .nav-cta:hover {transform:scale(1.05);box-shadow:0 0 24px rgba(0,0,0,0.25);}
  .nav-toggle { display: none; flex-direction: column; justify-content: center; gap: 5px; width: 34px; height: 34px; background: none; border: none; cursor: pointer; padding: 0; }
  .nav-toggle span { display: block; width: 100%; height: 2px; background: var(--text); border-radius: 2px; }

  .container {max-width:720px;margin:0 auto;padding:3rem 2rem 4rem;}
  .page-header {margin-bottom:2.5rem;}
  .page-header h1 {font-size:2rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.25rem;color:var(--text);}
  .page-header .updated {font-size:0.8rem;color:var(--text-dim);}

  .legal-card {
    background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
    padding:2.5rem 2rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);
  }
  .legal-card h2 {
    font-size:0.95rem;font-weight:700;color:var(--text);
    margin:2rem 0 0.75rem;padding-bottom:0.5rem;
    border-bottom:1px solid var(--border);
  }
  .legal-card h2:first-child {margin-top:0;}
  .legal-card p, .legal-card li {
    font-size:0.85rem;line-height:1.8;color:var(--text-muted);margin-bottom:0.5rem;
  }
  .legal-card ul {padding-left:1.25rem;margin:0.5rem 0 0.75rem;}
  .legal-card ul li {list-style:disc;margin-bottom:0.35rem;}
  .legal-card strong {color:var(--text);font-weight:600;}
  .legal-card .emphasis {
    background:var(--accent-tint);border-left:3px solid var(--accent);
    padding:1rem 1.25rem;margin:1rem 0;border-radius:0 var(--radius-sm) var(--radius-sm) 0;
    font-size:0.85rem;color:var(--text);line-height:1.7;
  }
  .section-num {color:var(--accent-dark);font-weight:700;margin-right:0.25rem;}
  .foot {text-align:center;margin-top:2rem;font-size:0.8rem;color:var(--text-dim);}
  .foot a {color:var(--accent-dark);}
  .foot a:hover {text-decoration:underline;}

  @media(max-width:600px) {
    .container {padding:2rem 1rem 3rem;}
    .legal-card {padding:1.5rem 1.25rem;}
    .nav-toggle { display: flex; }
    .nav-links {
      display: none; position: absolute; top: 100%; left: 0; right: 0;
      flex-direction: column; gap: 0; padding: 0.5rem 1.25rem 1.25rem;
      background: #fff; border-bottom: 1px solid rgba(15,31,47,0.08);
    }
    .nav-links.open { display: flex; }
    .nav-links a { padding: 0.75rem 0; border-bottom: 1px solid rgba(15,31,47,0.08); }
    .nav-links a:last-child { border-bottom: none; }
  }
</style>
</head>
<body>
<nav class="nav">
  <a href="/" class="nav-left">
    <div class="nav-logo"><img src="/static/logo.svg" alt="TxtAnOffer"></div>
    <span>TxtAnOffer</span>
  </a>
  <div class="nav-links" id="navLinks">
    <a href="/#how">How it works</a>
    <a href="/#trust">Accuracy</a>
    <a href="/pricing">Pricing</a>
    <a href="/demo">Demo</a>
    <a href="/playground">Parser Playground</a>
    <a href="/faq">FAQ</a>
    <a href="/about">About</a>
    <a href="/contact">Support</a>
    <a href="/login">Log In</a>
  </div>
  <a href="/signup" class="nav-cta">Start Free Trial</a>
  <button class="nav-toggle" id="navToggle" aria-label="Menu" aria-expanded="false"><span></span><span></span><span></span></button>
</nav>
<script>
(function(){
  var t=document.getElementById('navToggle'), l=document.getElementById('navLinks');
  if(!t||!l) return;
  t.addEventListener('click', function(){
    var open = l.classList.toggle('open');
    t.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  l.querySelectorAll('a').forEach(function(a){
    a.addEventListener('click', function(){ l.classList.remove('open'); t.setAttribute('aria-expanded','false'); });
  });
})();
</script>

<div class="container">
  <div class="page-header">
    <h1>Terms of Service</h1>
    <span class="updated">Last Updated: August 12, 2026</span>
  </div>

  <div class="legal-card">
    <p>These Terms of Service ("Terms") govern your use of TxtAnOffer ("Service"), operated by Phanel ("we," "us," or "our"), a sole proprietorship based in Texas. By accessing or using the Service, you agree to be bound by these Terms. If you do not agree, do not use the Service.</p>

    <h2><span class="section-num">1.</span> Service Description</h2>
    <p>TxtAnOffer is a document drafting tool that converts shorthand offer text into pre-filled TREC One to Four Family Residential Contract (Resale) forms (TREC No. 20-19). The Service accepts offer parameters via SMS or a web interface and generates a partially completed PDF contract for review by a licensed Texas real estate agent.</p>
    <p>The Service fills in standard TREC form fields based on information you provide. It does not create custom legal documents, negotiate terms, or exercise professional judgment on your behalf.</p>

    <h2><span class="section-num">2.</span> Not Legal Advice — No Attorney-Client Relationship</h2>
    <div class="emphasis">
      TxtAnOffer is NOT a law firm, does NOT provide legal advice, and does NOT serve as a substitute for consultation with a licensed attorney. No attorney-client relationship is formed by your use of the Service.
    </div>
    <p>The Service performs mechanical form-filling only. It does not:</p>
    <ul>
      <li>Interpret or advise on the legal effect of any contract term</li>
      <li>Evaluate whether a particular offer is appropriate, enforceable, or in your best interest</li>
      <li>Replace the judgment of a qualified real estate attorney</li>
      <li>Provide guidance on TREC rules, disclosure requirements, or regulatory compliance</li>
    </ul>
    <p>We strongly recommend that all generated documents be reviewed by a licensed Texas attorney before execution, particularly for complex transactions, commercial properties, or situations involving material contingencies.</p>

    <h2><span class="section-num">3.</span> Draft Documents — Agent Responsibility</h2>
    <div class="emphasis">
      All documents generated by TxtAnOffer are DRAFTS only. You, the licensed real estate agent, are solely responsible for reviewing, verifying, and approving every field, calculation, date, and term before presenting any document to clients or counterparties.
    </div>
    <p>You acknowledge and agree that:</p>
    <ul>
      <li>Generated PDFs are incomplete working drafts, not final contracts</li>
      <li>Many fields are intentionally left blank for you to complete (buyer/seller names, earnest money, option fees, financing terms, etc.)</li>
      <li>You must independently verify that all auto-filled information — including property address, sales price, and closing date — is accurate and correctly placed</li>
      <li>You bear full professional responsibility for any document you sign, present, or transmit, regardless of whether it was generated by the Service</li>
      <li>The Service may misparse input, calculate dates incorrectly, or fill fields in error — it is your duty to catch and correct any such issues</li>
    </ul>
    <p>TxtAnOffer does not carry Errors &amp; Omissions (E&amp;O) insurance. Any E&amp;O coverage applicable to a transaction is your own policy as a licensed real estate agent, and it is your responsibility — not ours — to review and stand behind every document before it is presented or signed.</p>

    <h2><span class="section-num">4.</span> No Liability for Errors</h2>
    <p>We make no warranty, express or implied, that the Service will produce accurate, complete, or error-free documents. Without limitation, we disclaim all liability for:</p>
    <ul>
      <li>Errors in parsing your input text (price, percentages, dates, addresses)</li>
      <li>Incorrect placement of data in PDF form fields</li>
      <li>Mathematical or date calculation errors</li>
      <li>PDF rendering issues, corrupted files, or formatting problems</li>
      <li>Use of an outdated form version if TREC revises the 20-19 form</li>
      <li>Any downstream consequence of relying on a generated draft without independent review</li>
    </ul>
    <p>THE SERVICE IS PROVIDED "AS IS" AND "AS AVAILABLE" WITHOUT WARRANTIES OF ANY KIND, WHETHER EXPRESS, IMPLIED, STATUTORY, OR OTHERWISE, INCLUDING WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND NON-INFRINGEMENT.</p>

    <h2><span class="section-num">5.</span> TREC Disclaimer</h2>
    <p>TxtAnOffer is an independent, third-party tool. We are NOT affiliated with, endorsed by, or partnered with the Texas Real Estate Commission (TREC) in any capacity. "TREC" and the form numbers referenced herein are trademarks or designations of the Texas Real Estate Commission.</p>
    <p>We use publicly available TREC promulgated forms as templates. The template currently in use is TREC 20-19, mandatory as of __TREC_FORM_DATE__. If TREC revises or replaces a form, there may be a delay before we update the Service. You are responsible for confirming that the form version used is current and appropriate for your transaction.</p>

    <h2><span class="section-num">6.</span> Subscription, Payment, and Cancellation</h2>
    <p><strong>Pricing:</strong> Plans start at $39.00 per month, billed monthly via Stripe. See <a href="/pricing" style="color:var(--accent-light);">pricing page</a> for current tiers.</p>
    <p><strong>Billing cycle:</strong> Your subscription renews automatically on the same date each month. You will be charged at the beginning of each billing period.</p>
    <p><strong>Cancellation:</strong> You may cancel your subscription at any time through your account settings or by contacting us. Cancellation takes effect at the end of your current billing period — you retain access until that date.</p>
    <p><strong>Refunds:</strong> Payments are non-refundable. We do not provide prorated refunds for partial months. If you cancel mid-cycle, you retain access through the remainder of the paid period but will not receive a refund for unused time.</p>
    <p><strong>Price changes:</strong> We reserve the right to modify pricing with 30 days' written notice (via email or SMS). Continued use of the Service after a price change constitutes acceptance of the new price.</p>
    <p><strong>Failed payments:</strong> If a payment fails, we may suspend access to the Service until the balance is resolved. We are not responsible for any disruption caused by payment failures.</p>

    <h2><span class="section-num">7.</span> Limitation of Liability</h2>
    <p>TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW, IN NO EVENT SHALL TXTANOFFER, ITS OWNER, OPERATORS, OR AFFILIATES BE LIABLE FOR ANY INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, OR PUNITIVE DAMAGES, INCLUDING WITHOUT LIMITATION:</p>
    <ul>
      <li>Loss of profits, revenue, or business opportunities</li>
      <li>Loss of a transaction, deal, or commission</li>
      <li>Costs of procuring substitute services</li>
      <li>Damages arising from errors in generated documents</li>
      <li>Damages arising from service interruptions or downtime</li>
    </ul>
    <p>OUR TOTAL AGGREGATE LIABILITY FOR ANY CLAIMS ARISING FROM OR RELATED TO THE SERVICE SHALL NOT EXCEED THE AMOUNT YOU PAID TO US IN THE THREE (3) MONTHS IMMEDIATELY PRECEDING THE EVENT GIVING RISE TO THE CLAIM.</p>
    <p>This limitation applies regardless of the legal theory (contract, tort, strict liability, or otherwise) and even if we have been advised of the possibility of such damages.</p>

    <h2><span class="section-num">8.</span> Indemnification</h2>
    <p>You agree to indemnify, defend, and hold harmless TxtAnOffer, its owner, and any contractors from and against any and all claims, damages, losses, liabilities, costs, and expenses (including reasonable attorneys' fees) arising out of or related to:</p>
    <ul>
      <li>Your use of the Service or any documents generated by the Service</li>
      <li>Any transaction in which a document generated by the Service is used</li>
      <li>Your failure to review, verify, or correct generated documents before use</li>
      <li>Your violation of these Terms</li>
      <li>Your violation of any applicable law, regulation, or third-party right</li>
      <li>Any claim brought by your clients, counterparties, or their representatives in connection with a generated document</li>
    </ul>

    <h2><span class="section-num">9.</span> Data Handling and Privacy</h2>
    <p>In the course of providing the Service, we collect and store:</p>
    <ul>
      <li>Your phone number (for SMS-based interactions)</li>
      <li>Agent profile information you provide</li>
      <li>Offer text messages you send to the Service</li>
      <li>Generated PDF documents (temporarily, for download)</li>
      <li>Basic usage data (timestamps, request counts)</li>
    </ul>
    <p>We use this data solely to operate and improve the Service. We do not sell your personal information to third parties.</p>
    <p><strong>Third-party services:</strong> The Service uses Twilio (SMS delivery), Stripe (payment processing), and Railway on Google Cloud Platform (infrastructure). These services have their own privacy policies and may process your data in accordance with their terms.</p>
    <p><strong>Data retention:</strong> Generated PDFs are stored temporarily and may be deleted after a reasonable period (currently 30 days). We retain account and billing records as required by law. <strong>This is shorter than the 4-year offer/contract/addenda retention period brokers are independently required to maintain under TREC Rule &sect;535.2.</strong> Our retention does not satisfy that obligation &mdash; you are responsible for downloading and separately retaining your own copy of every offer and amendment.</p>
    <p><strong>Security:</strong> We implement reasonable technical and organizational measures to protect your data. However, no system is perfectly secure, and we cannot guarantee absolute security of your information.</p>

    <h2><span class="section-num">10.</span> Acceptable Use</h2>
    <p>You agree not to:</p>
    <ul>
      <li>Use the Service for any unlawful purpose</li>
      <li>Submit false, fraudulent, or misleading information</li>
      <li>Attempt to reverse-engineer, decompile, or extract the source code of the Service</li>
      <li>Resell, redistribute, or sublicense access to the Service without our written consent</li>
      <li>Use automated tools to send excessive requests that degrade service quality</li>
      <li>Represent generated drafts as attorney-reviewed or finalized legal documents</li>
    </ul>

    <h2><span class="section-num">11.</span> Governing Law and Dispute Resolution</h2>
    <p><strong>Governing law:</strong> These Terms shall be governed by and construed in accordance with the laws of the State of Texas, without regard to its conflict-of-law provisions.</p>
    <p><strong>Jurisdiction:</strong> Any legal action or proceeding arising out of or relating to these Terms or the Service shall be brought exclusively in the state or federal courts located in Texas, and you consent to the personal jurisdiction of such courts.</p>
    <p><strong>Informal resolution:</strong> Before filing any formal legal proceeding, you agree to attempt to resolve any dispute informally by contacting us. We will attempt to resolve the dispute within 30 days of receiving your notice.</p>

    <h2><span class="section-num">12.</span> Modifications to Terms</h2>
    <p>We reserve the right to modify these Terms at any time. Changes will be effective upon posting to this page with an updated "Last Updated" date. Your continued use of the Service after changes are posted constitutes acceptance of the revised Terms.</p>
    <p>For material changes (including pricing changes), we will provide at least 30 days' notice via email or SMS before the changes take effect.</p>

    <h2><span class="section-num">13.</span> Termination</h2>
    <p>We may suspend or terminate your access to the Service at any time, with or without cause, and with or without notice. Upon termination, your right to use the Service ceases immediately. Sections 2, 3, 4, 7, 8, 9, and 11 survive termination.</p>

    <h2><span class="section-num">14.</span> Contact</h2>
    <p>For questions about these Terms or the Service, contact us at:</p>
    <p>TxtAnOffer<br>Operated by Phanel<br>Texas, United States<br>Email: support@txtanoffer.com</p>
  </div>
  <p class="foot">TxtAnOffer is not affiliated with the Texas Real Estate Commission (TREC).<br><a href="/">&larr; Back to home</a> &middot; <a href="/privacy">Privacy Policy</a></p>
</div>
</body>
</html>"""
    return html.replace("__TREC_FORM_DATE__", TREC_FORM_CURRENT_AS_OF)


@app.route("/privacy")
def privacy():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Privacy Policy — TxtAnOffer</title>
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
  :root {
    --bg: #F5F5F7;
    --bg-card: #fff;
    --border: rgba(15,31,47,0.08);
    --border-hover: rgba(0,0,0,0.35);
    --text: #0f1f2f;
    --text-muted: #5a6b7a;
    --text-dim: #8a9aa9;
    --accent: #171717;
    --accent-light: #525252;
    --accent-dark: #000000;
    --accent-tint: #F0F0EE;
    --radius: 1.25rem;
    --radius-sm: 0.85rem;
    --transition: all 0.2s ease;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
    background:var(--bg);
    color:var(--text);
    line-height:1.5;
    -webkit-font-smoothing:antialiased;
    min-height:100vh;
  }
  a { color:inherit; text-decoration:none; }

  .nav {
    display:flex;align-items:center;justify-content:space-between;
    padding:1rem 2rem;position:sticky;top:0;
    background:rgba(255,255,255,0.85);backdrop-filter:blur(20px);
    -webkit-backdrop-filter:blur(20px);
    border-bottom:1px solid var(--border);z-index:100;
  }
  .nav-left {display:flex;align-items:center;gap:0.6rem;font-weight:700;font-size:1.1rem;letter-spacing:-0.02em;color:var(--text);}
  .nav-logo {width:34px;height:34px;border-radius:22%;overflow:hidden;}
  .nav-logo img {width:100%;height:100%;object-fit:contain;}
  .nav-links {display:flex;gap:2rem;font-size:0.875rem;font-weight:500;color:var(--text-muted);}
  .nav-links a {transition:var(--transition);}
  .nav-links a:hover {color:var(--text);}
  .nav-cta {
    background:var(--accent);color:#fff;padding:0.55rem 1.35rem;border-radius:9999px;
    font-size:0.875rem;font-weight:600;text-decoration:none;display:inline-block;
    transition:var(--transition);
  }
  .nav-cta:hover {transform:scale(1.05);box-shadow:0 0 24px rgba(0,0,0,0.25);}
  .nav-toggle { display: none; flex-direction: column; justify-content: center; gap: 5px; width: 34px; height: 34px; background: none; border: none; cursor: pointer; padding: 0; }
  .nav-toggle span { display: block; width: 100%; height: 2px; background: var(--text); border-radius: 2px; }

  .container {max-width:720px;margin:0 auto;padding:3rem 2rem 4rem;}
  .page-header {margin-bottom:2.5rem;}
  .page-header h1 {font-size:2rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.25rem;color:var(--text);}
  .page-header .updated {font-size:0.8rem;color:var(--text-dim);}

  .legal-card {
    background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
    padding:2.5rem 2rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);
  }
  .legal-card h2 {
    font-size:0.95rem;font-weight:700;color:var(--text);
    margin:2rem 0 0.75rem;padding-bottom:0.5rem;
    border-bottom:1px solid var(--border);
  }
  .legal-card h2:first-child {margin-top:0;}
  .legal-card p, .legal-card li {
    font-size:0.85rem;line-height:1.8;color:var(--text-muted);margin-bottom:0.5rem;
  }
  .legal-card ul {padding-left:1.25rem;margin:0.5rem 0 0.75rem;}
  .legal-card ul li {list-style:disc;margin-bottom:0.35rem;}
  .legal-card strong {color:var(--text);font-weight:600;}
  .foot {text-align:center;margin-top:2rem;font-size:0.8rem;color:var(--text-dim);}
  .foot a {color:var(--accent-dark);}
  .foot a:hover {text-decoration:underline;}

  @media(max-width:600px) {
    .container {padding:2rem 1rem 3rem;}
    .legal-card {padding:1.5rem 1.25rem;}
    .nav-toggle { display: flex; }
    .nav-links {
      display: none; position: absolute; top: 100%; left: 0; right: 0;
      flex-direction: column; gap: 0; padding: 0.5rem 1.25rem 1.25rem;
      background: #fff; border-bottom: 1px solid rgba(15,31,47,0.08);
    }
    .nav-links.open { display: flex; }
    .nav-links a { padding: 0.75rem 0; border-bottom: 1px solid rgba(15,31,47,0.08); }
    .nav-links a:last-child { border-bottom: none; }
  }
</style>
</head>
<body>
<nav class="nav">
  <a href="/" class="nav-left">
    <div class="nav-logo"><img src="/static/logo.svg" alt="TxtAnOffer"></div>
    <span>TxtAnOffer</span>
  </a>
  <div class="nav-links" id="navLinks">
    <a href="/#how">How it works</a>
    <a href="/#trust">Accuracy</a>
    <a href="/pricing">Pricing</a>
    <a href="/demo">Demo</a>
    <a href="/playground">Parser Playground</a>
    <a href="/faq">FAQ</a>
    <a href="/about">About</a>
    <a href="/contact">Support</a>
    <a href="/login">Log In</a>
  </div>
  <a href="/signup" class="nav-cta">Start Free Trial</a>
  <button class="nav-toggle" id="navToggle" aria-label="Menu" aria-expanded="false"><span></span><span></span><span></span></button>
</nav>
<script>
(function(){
  var t=document.getElementById('navToggle'), l=document.getElementById('navLinks');
  if(!t||!l) return;
  t.addEventListener('click', function(){
    var open = l.classList.toggle('open');
    t.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  l.querySelectorAll('a').forEach(function(a){
    a.addEventListener('click', function(){ l.classList.remove('open'); t.setAttribute('aria-expanded','false'); });
  });
})();
</script>

<div class="container">
  <div class="page-header">
    <h1>Privacy Policy</h1>
    <span class="updated">Last Updated: July 14, 2026</span>
  </div>

  <div class="legal-card">
    <p>TxtAnOffer ("Service") is operated by Phanel, a sole proprietorship based in Texas. This Privacy Policy explains how we collect, use, and protect your information.</p>

    <h2>1. Information We Collect</h2>
    <p><strong>Information you provide:</strong></p>
    <ul>
      <li>Phone number (for SMS interactions and account identification)</li>
      <li>Agent profile details (name, license number, brokerage, email)</li>
      <li>Offer text messages and form submissions</li>
      <li>Payment information (processed securely by Stripe; we do not store card numbers)</li>
    </ul>
    <p><strong>Information collected automatically:</strong></p>
    <ul>
      <li>Usage data (timestamps, request counts, feature usage)</li>
      <li>Device and browser information when using the web interface</li>
      <li>IP address</li>
    </ul>

    <h2>2. How We Use Your Information</h2>
    <ul>
      <li>To provide the Service: parsing offers, generating PDFs, delivering SMS responses</li>
      <li>To manage your account and subscription</li>
      <li>To improve and maintain the Service</li>
      <li>To communicate with you about your account or the Service</li>
      <li>To comply with legal obligations</li>
    </ul>

    <h2 id="sms-messaging">3. SMS Messaging</h2>
    <p><strong>Program Name:</strong> TxtAnOffer</p>
    <p><strong>Phone Number:</strong> +1 (833) 897-0333</p>
    <p><strong>Opt-in Method:</strong> Users opt in by (1) entering their phone number and checking an unchecked checkbox on www.txtanoffer.com/signup that says "By checking this box, I agree to receive automated transactional SMS messages from TxtAnOffer at +1 (833) 897-0333 about my offer drafts. Message frequency varies based on usage. Reply STOP to opt out, HELP for help. Msg &amp; data rates may apply. Consent is not a condition of purchase." OR (2) by texting offer details directly to +1 (833) 897-0333 after seeing opt-in disclosure on our website.</p>
    <p><strong>Consent:</strong> By texting our service number +1 (833) 897-0333 or submitting your phone number via our website, you consent to receive SMS messages from TxtAnOffer related to your offer requests and account.</p>
    <p><strong>Message frequency:</strong> Message frequency varies based on your usage. You will receive one response per offer submitted, plus occasional account notifications (typically 1-5 messages per month), including a one-time reminder a few days before the closing date of an offer you generated.</p>
    <p><strong>Opt-out:</strong> Reply STOP to any message to unsubscribe from SMS. Reply START to re-subscribe. You can continue using the web interface after opting out of SMS.</p>
    <p><strong>Help:</strong> Reply HELP for support information, or contact support@txtanoffer.com or +1 (833) 897-0333.</p>
    <p><strong>Rates:</strong> Message and data rates may apply depending on your carrier plan.</p>
    <p><strong>Carriers:</strong> Compatible with all major US carriers. Carriers are not liable for delayed or undelivered messages.</p>
    <p>This is a transactional service tied to offers you generate -- most messages are user-initiated, plus the closing-date reminder described above. We do not send marketing or promotional messages.</p>

    <h2>4. Data Sharing</h2>
    <p>We do not sell, rent, or trade your personal information. We share data only with:</p>
    <ul>
      <li><strong>Twilio</strong> — SMS delivery (phone number, message content)</li>
      <li><strong>Stripe</strong> — Payment processing (billing details)</li>
      <li><strong>Railway (hosted on Google Cloud Platform)</strong> — Infrastructure provider, SOC 2 Type II certified. All data encrypted in transit (TLS 1.3) and at rest (AES-256). US region only.</li>
    </ul>
    <p>We may disclose information if required by law, legal process, or to protect the rights and safety of our users or the public.</p>

    <h2>5. Data Retention</h2>
    <ul>
      <li>Generated PDFs: stored temporarily for download, deleted after 30 days</li>
      <li>Account data: retained while your account is active and for 90 days after cancellation</li>
      <li>Billing records: retained as required by applicable tax and accounting laws</li>
      <li>SMS logs: retained for 90 days for support and debugging purposes</li>
    </ul>
    <p><strong>Note for licensees:</strong> TREC Rule &sect;535.2 requires brokers to independently retain offers, contracts, and related addenda for at least 4 years from closing or termination. Our 30-day PDF retention does not satisfy that requirement &mdash; you are responsible for downloading and separately retaining your own copy of every offer and amendment.</p>

    <h2>6. Data Security</h2>
    <p>We implement reasonable technical and organizational measures to protect your data:</p>
    <ul>
      <li><strong>Encryption in transit:</strong> TLS 1.3 on all connections</li>
      <li><strong>Encryption at rest:</strong> AES-256 via Google Cloud Platform infrastructure</li>
      <li><strong>Infrastructure:</strong> Railway (SOC 2 Type II certified), running on GCP (SOC 2, ISO 27001)</li>
      <li><strong>Access controls:</strong> No human access to offer content — all processing is automated</li>
      <li><strong>Payment data:</strong> Handled exclusively by Stripe (PCI DSS Level 1); card numbers never touch our servers</li>
    </ul>
    <p>No method of transmission over the internet is 100% secure, and we cannot guarantee absolute security.</p>

    <h2>7. Your Rights</h2>
    <p>You may:</p>
    <ul>
      <li>Request access to your personal data</li>
      <li>Request correction or deletion of your data</li>
      <li>Opt out of SMS communications (reply STOP)</li>
      <li>Cancel your subscription at any time</li>
    </ul>
    <p>To exercise these rights, contact us at support@txtanoffer.com.</p>

    <h2>8. Children's Privacy</h2>
    <p>The Service is intended for licensed real estate professionals and is not directed at individuals under 18. We do not knowingly collect information from minors.</p>

    <h2>9. Changes to This Policy</h2>
    <p>We may update this Privacy Policy from time to time. Changes will be posted on this page with an updated "Last Updated" date. Continued use of the Service after changes constitutes acceptance.</p>

    <h2>10. Contact</h2>
    <p>For privacy-related questions or requests:</p>
    <p>TxtAnOffer<br>Operated by Phanel<br>Texas, United States<br>Email: support@txtanoffer.com</p>
  </div>
  <p class="foot">TxtAnOffer is not affiliated with the Texas Real Estate Commission (TREC).<br><a href="/">&larr; Back to home</a> &middot; <a href="/terms">Terms of Service</a></p>
</div>
</body>
</html>"""


@app.route("/faq")
def faq():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FAQ — TxtAnOffer</title>
<meta name="description" content="Answers to common questions about TxtAnOffer: TREC affiliation, parser accuracy, data retention, and what the Service does and doesn't cover.">
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
  :root {
    --bg: #F5F5F7;
    --bg-card: #fff;
    --border: rgba(15,31,47,0.08);
    --text: #0f1f2f;
    --text-muted: #5a6b7a;
    --text-dim: #8a9aa9;
    --accent: #171717;
    --accent-light: #525252;
    --accent-dark: #000000;
    --accent-tint: #F0F0EE;
    --radius: 1.25rem;
    --radius-sm: 0.85rem;
    --transition: all 0.2s ease;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
    background:var(--bg); color:var(--text); line-height:1.5;
    -webkit-font-smoothing:antialiased; min-height:100vh;
  }
  a { color:inherit; text-decoration:none; }
  .nav {
    display:flex;align-items:center;justify-content:space-between;
    padding:1rem 2rem;position:sticky;top:0;
    background:rgba(255,255,255,0.85);backdrop-filter:blur(20px);
    -webkit-backdrop-filter:blur(20px);
    border-bottom:1px solid var(--border);z-index:100;
  }
  .nav-left {display:flex;align-items:center;gap:0.6rem;font-weight:700;font-size:1.1rem;letter-spacing:-0.02em;color:var(--text);}
  .nav-logo {width:34px;height:34px;border-radius:22%;overflow:hidden;}
  .nav-logo img {width:100%;height:100%;object-fit:contain;}
  .nav-links {display:flex;gap:2rem;font-size:0.875rem;font-weight:500;color:var(--text-muted);}
  .nav-links a {transition:var(--transition);}
  .nav-links a:hover {color:var(--text);}
  .nav-cta {
    background:var(--accent);color:#fff;padding:0.55rem 1.35rem;border-radius:9999px;
    font-size:0.875rem;font-weight:600;text-decoration:none;display:inline-block;
    transition:var(--transition);
  }
  .nav-cta:hover {transform:scale(1.05);box-shadow:0 0 24px rgba(0,0,0,0.25);}
  .nav-toggle { display: none; flex-direction: column; justify-content: center; gap: 5px; width: 34px; height: 34px; background: none; border: none; cursor: pointer; padding: 0; }
  .nav-toggle span { display: block; width: 100%; height: 2px; background: var(--text); border-radius: 2px; }
  .container {max-width:720px;margin:0 auto;padding:3rem 2rem 4rem;}
  .page-header {margin-bottom:2.5rem;}
  .page-header h1 {font-size:2rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.25rem;color:var(--text);}
  .page-header p {font-size:0.9rem;color:var(--text-muted);}
  .faq-item {
    background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
    padding:1.5rem 1.75rem;margin-bottom:1rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);
  }
  .faq-item h2 {font-size:1rem;font-weight:700;margin-bottom:0.6rem;color:var(--text);}
  .faq-item p {font-size:0.85rem;line-height:1.75;color:var(--text-muted);}
  .faq-item p + p {margin-top:0.5rem;}
  .faq-item strong {color:var(--text);font-weight:600;}
  .foot {text-align:center;margin-top:2rem;font-size:0.8rem;color:var(--text-dim);}
  .foot a {color:var(--accent-dark);}
  .foot a:hover {text-decoration:underline;}
  @media(max-width:600px) {
    .container {padding:2rem 1rem 3rem;}
    .faq-item {padding:1.25rem 1.25rem;}
    .nav-toggle { display: flex; }
    .nav-links {
      display: none; position: absolute; top: 100%; left: 0; right: 0;
      flex-direction: column; gap: 0; padding: 0.5rem 1.25rem 1.25rem;
      background: #fff; border-bottom: 1px solid rgba(15,31,47,0.08);
    }
    .nav-links.open { display: flex; }
    .nav-links a { padding: 0.75rem 0; border-bottom: 1px solid rgba(15,31,47,0.08); }
    .nav-links a:last-child { border-bottom: none; }
  }
</style>
</head>
<body>
<nav class="nav">
  <a href="/" class="nav-left">
    <div class="nav-logo"><img src="/static/logo.svg" alt="TxtAnOffer"></div>
    <span>TxtAnOffer</span>
  </a>
  <div class="nav-links" id="navLinks">
    <a href="/#how">How it works</a>
    <a href="/#trust">Accuracy</a>
    <a href="/pricing">Pricing</a>
    <a href="/demo">Demo</a>
    <a href="/playground">Parser Playground</a>
    <a href="/faq">FAQ</a>
    <a href="/about">About</a>
    <a href="/contact">Support</a>
    <a href="/login">Log In</a>
  </div>
  <a href="/signup" class="nav-cta">Start Free Trial</a>
  <button class="nav-toggle" id="navToggle" aria-label="Menu" aria-expanded="false"><span></span><span></span><span></span></button>
</nav>
<script>
(function(){
  var t=document.getElementById('navToggle'), l=document.getElementById('navLinks');
  if(!t||!l) return;
  t.addEventListener('click', function(){
    var open = l.classList.toggle('open');
    t.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  l.querySelectorAll('a').forEach(function(a){
    a.addEventListener('click', function(){ l.classList.remove('open'); t.setAttribute('aria-expanded','false'); });
  });
})();
</script>

<div class="container">
  <div class="page-header">
    <h1>Frequently Asked Questions</h1>
    <p>Straight answers about what TxtAnOffer does, what it doesn't, and how your data is handled.</p>
  </div>

  <div class="faq-item">
    <h2>Is TxtAnOffer officially approved by TREC?</h2>
    <p><strong>No</strong> &mdash; and that's important. TxtAnOffer is an independent tool that fills publicly available TREC promulgated forms (currently TREC 20-19, mandatory as of __TREC_FORM_DATE__). We are not affiliated with, endorsed by, or partnered with the Texas Real Estate Commission. You, the licensed agent, are responsible for reviewing every field before signing.</p>
  </div>

  <div class="faq-item">
    <h2>What if the parser gets a number wrong?</h2>
    <p>Every generated PDF is a draft. You must review all fields &mdash; price, dates, address, percentages &mdash; before presenting to clients. The parser is highly accurate, but you are the final check. Fields like buyer/seller names, earnest money, and financing terms are intentionally left blank for you to complete.</p>
  </div>

  <div class="faq-item">
    <h2>Can I amend an offer after I've already sent it?</h2>
    <p>Yes &mdash; text <strong>AMEND &lt;address&gt; price &lt;value&gt;</strong> or <strong>AMEND &lt;address&gt; close +&lt;days&gt;</strong> (e.g. <em>"AMEND 123 Main St price 730k"</em> or <em>"AMEND 123 Main St close +10"</em>) and you'll get back a filled TREC 39-11 Amendment for that contract. It's included on every plan, works the same way in the <a href="/demo" style="color:var(--accent-dark);">web demo</a>, and shows up nested under the original offer on your <strong>Dashboard</strong>. Only the price or closing-date field you asked to change is filled &mdash; everything else on the form is left blank for you to complete, same as the main contract.</p>
  </div>

  <div class="faq-item">
    <h2>Do you store my texts or offers?</h2>
    <p>Generated PDFs are stored temporarily for download and deleted after 30 days. SMS logs are retained for 90 days for support and debugging. We do not sell or share your data. See our <a href="/privacy" style="color:var(--accent-dark);">Privacy Policy</a> for the full breakdown.</p>
    <p><strong>Important:</strong> TREC Rule &sect;535.2 requires brokers to independently retain records of offers, contracts, and related addenda for at least 4 years from closing or termination of the transaction. Our 30-day retention does not satisfy that requirement &mdash; download and save your own copy of every offer and amendment PDF.</p>
  </div>

  <div class="faq-item">
    <h2>Can I use this for commercial properties or new construction?</h2>
    <p>TxtAnOffer is designed for residential resale using TREC Form 20-19. For commercial, new construction, or complex transactions, consult a Texas real estate attorney.</p>
  </div>

  <div class="faq-item">
    <h2>Will I get reminded before an offer's closing date?</h2>
    <p>Yes &mdash; TxtAnOffer sends a one-time text a few days before the closing date of an offer you generated, so it doesn't slip past you. This is the one message we send without you texting first; reply STOP anytime to opt out of all messages, including this one.</p>
  </div>

  <div class="faq-item">
    <h2>What happens if my text doesn't go through?</h2>
    <p>You'll receive a confirmation reply for every offer received, generally within seconds. If you don't get one within 30 seconds, try again or use the <a href="/demo" style="color:var(--accent-dark);">web interface</a> at txtanoffer.com/demo.</p>
  </div>

  <div class="faq-item">
    <h2>Do I need E&amp;O insurance to use TxtAnOffer?</h2>
    <p>TxtAnOffer does not carry Errors &amp; Omissions insurance. Any E&amp;O coverage applicable to a transaction is your own policy as a licensed agent &mdash; it's your responsibility to review and stand behind every document you present or sign. See <a href="/terms" style="color:var(--accent-dark);">Terms of Service</a> for details.</p>
  </div>

  <p class="foot">Still have a question? Email <a href="mailto:support@txtanoffer.com">support@txtanoffer.com</a>.<br><a href="/">&larr; Back to home</a> &middot; <a href="/terms">Terms</a> &middot; <a href="/privacy">Privacy Policy</a></p>
</div>
</body>
</html>"""
    return html.replace("__TREC_FORM_DATE__", TREC_FORM_CURRENT_AS_OF)


@app.route("/trec-changes")
def trec_changes():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TREC Contract Changes — TxtAnOffer</title>
<meta name="description" content="What's changed on the TREC 20-19 One to Four Family Residential Contract -- Paragraph 12B commission language, the mandatory Water Disclosure, the HOA addendum, and how TxtAnOffer keeps every generated contract current.">
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
  :root {
    --bg: #F5F5F7;
    --bg-card: #fff;
    --border: rgba(15,31,47,0.08);
    --text: #0f1f2f;
    --text-muted: #5a6b7a;
    --text-dim: #8a9aa9;
    --accent: #171717;
    --accent-light: #525252;
    --accent-dark: #000000;
    --accent-tint: #F0F0EE;
    --green: #10b981;
    --green-tint: #E7F7F1;
    --radius: 1.25rem;
    --radius-sm: 0.85rem;
    --transition: all 0.2s ease;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
    background:var(--bg); color:var(--text); line-height:1.5;
    -webkit-font-smoothing:antialiased; min-height:100vh;
  }
  a { color:inherit; text-decoration:none; }
  .nav {
    display:flex;align-items:center;justify-content:space-between;
    padding:1rem 2rem;position:sticky;top:0;
    background:rgba(255,255,255,0.85);backdrop-filter:blur(20px);
    -webkit-backdrop-filter:blur(20px);
    border-bottom:1px solid var(--border);z-index:100;
  }
  .nav-left {display:flex;align-items:center;gap:0.6rem;font-weight:700;font-size:1.1rem;letter-spacing:-0.02em;color:var(--text);}
  .nav-logo {width:34px;height:34px;border-radius:22%;overflow:hidden;}
  .nav-logo img {width:100%;height:100%;object-fit:contain;}
  .nav-links {display:flex;gap:1.75rem;font-size:0.875rem;font-weight:500;color:var(--text-muted);}
  .nav-links a {transition:var(--transition);}
  .nav-links a:hover {color:var(--text);}
  .nav-cta {
    background:var(--accent);color:#fff;padding:0.55rem 1.35rem;border-radius:9999px;
    font-size:0.875rem;font-weight:600;text-decoration:none;display:inline-block;
    transition:var(--transition);
  }
  .nav-cta:hover {transform:scale(1.05);box-shadow:0 0 24px rgba(0,0,0,0.25);}
  .nav-toggle { display: none; flex-direction: column; justify-content: center; gap: 5px; width: 34px; height: 34px; background: none; border: none; cursor: pointer; padding: 0; }
  .nav-toggle span { display: block; width: 100%; height: 2px; background: var(--text); border-radius: 2px; }
  .container {max-width:760px;margin:0 auto;padding:3rem 2rem 4rem;}
  .page-header {margin-bottom:0.5rem;}
  .badge {
    display:inline-flex;align-items:center;gap:0.4rem;
    background:var(--green-tint);color:#067a5c;border:1px solid rgba(16,185,129,0.28);
    padding:0.35rem 0.85rem;border-radius:9999px;font-size:0.72rem;font-weight:700;
    text-transform:uppercase;letter-spacing:0.04em;margin-bottom:1rem;
  }
  .page-header h1 {font-size:2rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.5rem;color:var(--text);}
  .page-header p {font-size:0.95rem;color:var(--text-muted);max-width:60ch;}
  .last-verified {font-size:0.78rem;color:var(--text-dim);margin-top:1rem;margin-bottom:2.5rem;}
  .change-card {
    background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
    padding:1.75rem;margin-bottom:1.1rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);
  }
  .change-card .tag {
    display:inline-block;font-size:0.68rem;font-weight:700;letter-spacing:0.05em;
    text-transform:uppercase;color:#067a5c;background:var(--green-tint);
    padding:0.2rem 0.6rem;border-radius:0.4rem;margin-bottom:0.75rem;
  }
  .change-card h2 {font-size:1.05rem;font-weight:700;margin-bottom:0.6rem;color:var(--text);}
  .change-card p {font-size:0.87rem;line-height:1.7;color:var(--text-muted);}
  .change-card p + p {margin-top:0.6rem;}
  .change-card strong {color:var(--text);font-weight:600;}
  .change-card .handled {
    margin-top:0.9rem;padding-top:0.9rem;border-top:1px dashed var(--border);
    font-size:0.83rem;color:var(--text);
  }
  .change-card .handled b {color:#067a5c;}
  .sources-box {
    background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
    padding:1.5rem 1.75rem;margin-top:1.75rem;font-size:0.83rem;color:var(--text-muted);line-height:1.7;
  }
  .sources-box h3 {font-size:0.78rem;font-weight:700;text-transform:uppercase;letter-spacing:0.04em;color:var(--text);margin-bottom:0.7rem;}
  .sources-box ul {margin-left:1.1rem;}
  .sources-box li {margin-bottom:0.4rem;}
  .sources-box a {text-decoration:underline;color:var(--text);}
  .sources-box a:hover {color:#067a5c;}
  .disclaimer-box {
    background:var(--accent-tint);border:1px solid var(--border);border-radius:var(--radius);
    padding:1.5rem 1.75rem;margin-top:1.1rem;font-size:0.82rem;color:var(--text-muted);line-height:1.7;
  }
  .disclaimer-box strong {color:var(--text);}
  .cta-row {
    margin-top:2.5rem;padding:1.75rem;border-radius:var(--radius);
    background:var(--text);color:#fff;text-align:center;
  }
  .cta-row p {font-size:0.95rem;margin-bottom:1rem;color:rgba(255,255,255,0.85);}
  .cta-row a {
    display:inline-block;background:#fff;color:var(--text);padding:0.7rem 1.6rem;
    border-radius:9999px;font-weight:700;font-size:0.9rem;transition:var(--transition);
  }
  .cta-row a:hover {transform:scale(1.05);}
  .foot {text-align:center;margin-top:2rem;font-size:0.8rem;color:var(--text-dim);}
  .foot a {color:var(--accent-dark);}
  .foot a:hover {text-decoration:underline;}
  @media(max-width:600px) {
    .container {padding:2rem 1rem 3rem;}
    .change-card {padding:1.25rem 1.25rem;}
    .nav-toggle { display: flex; }
    .nav-links {
      display: none; position: absolute; top: 100%; left: 0; right: 0;
      flex-direction: column; gap: 0; padding: 0.5rem 1.25rem 1.25rem;
      background: #fff; border-bottom: 1px solid rgba(15,31,47,0.08);
    }
    .nav-links.open { display: flex; }
    .nav-links a { padding: 0.75rem 0; border-bottom: 1px solid rgba(15,31,47,0.08); }
    .nav-links a:last-child { border-bottom: none; }
  }
</style>
</head>
<body>
<nav class="nav">
  <a href="/" class="nav-left">
    <div class="nav-logo"><img src="/static/logo.svg" alt="TxtAnOffer"></div>
    <span>TxtAnOffer</span>
  </a>
  <div class="nav-links" id="navLinks">
    <a href="/#how">How it works</a>
    <a href="/#trust">Accuracy</a>
    <a href="/pricing">Pricing</a>
    <a href="/faq">FAQ</a>
    <a href="/about">About</a>
    <a href="/trec-changes">TREC Changes</a>
    <a href="/login">Log In</a>
  </div>
  <a href="/signup" class="nav-cta">Start Free Trial</a>
  <button class="nav-toggle" id="navToggle" aria-label="Menu" aria-expanded="false"><span></span><span></span><span></span></button>
</nav>
<script>
(function(){
  var t=document.getElementById('navToggle'), l=document.getElementById('navLinks');
  if(!t||!l) return;
  t.addEventListener('click', function(){
    var open = l.classList.toggle('open');
    t.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  l.querySelectorAll('a').forEach(function(a){
    a.addEventListener('click', function(){ l.classList.remove('open'); t.setAttribute('aria-expanded','false'); });
  });
})();
</script>

<div class="container">
  <div class="page-header">
    <div class="badge">&#9989; Mandatory as of __TREC_FORM_DATE__</div>
    <h1>What changed on the TREC 20-19</h1>
    <p>A plain-language rundown of what's currently required on the TREC One to Four Family Residential Contract (Resale) -- and exactly how TxtAnOffer handles each one automatically.</p>
  </div>
  <p class="last-verified">Last verified against the TREC-published form: __TREC_FORM_DATE__. This page is updated whenever the underlying form changes -- see the disclaimer below.</p>

  <div class="change-card">
    <span class="tag">Paragraph 12B</span>
    <h2>Broker compensation language</h2>
    <p>Following the industry-wide shift toward more explicit written buyer-broker compensation agreements, TREC's promulgated contract language in Paragraph 12 was updated. Every offer needs the current version of this paragraph, not a stale one from an old template sitting on someone's computer.</p>
    <div class="handled"><b>How TxtAnOffer handles it:</b> Every generated 20-19 is built from the current TREC-published form, so Paragraph 12B is always the current version -- no old templates, no manual tracking of which revision you're supposed to be using.</div>
  </div>

  <div class="change-card">
    <span class="tag">TREC 61-0</span>
    <h2>Seller's Disclosure re: Groundwater / Surface Water</h2>
    <p>This disclosure covers the seller's known groundwater and surface water rights on the property -- a mandatory attachment to the 20-19, not an optional add-on.</p>
    <div class="handled"><b>How TxtAnOffer handles it:</b> Attached automatically to every generated contract. It's not something you have to remember to add separately.</div>
  </div>

  <div class="change-card">
    <span class="tag">TREC 36-10</span>
    <h2>Addendum for Property Subject to Mandatory HOA Membership</h2>
    <p>Required whenever the property is subject to mandatory membership in a property owners association -- a common miss when a contract gets typed up quickly.</p>
    <div class="handled"><b>How TxtAnOffer handles it:</b> Mention an HOA anywhere in your text and the 36-10 addendum attaches itself automatically, checkbox and all.</div>
  </div>

  <div class="change-card">
    <span class="tag">TREC 40-11</span>
    <h2>Third Party Financing Addendum</h2>
    <p>Required whenever the offer isn't an all-cash deal -- financing type, loan terms, and buyer-approval sections all have to line up with the numbers in the main contract.</p>
    <div class="handled"><b>How TxtAnOffer handles it:</b> Attached automatically for financed offers (and correctly left off for all-cash ones), with the financing terms reconciled against the main contract's numbers.</div>
  </div>

  <div class="change-card">
    <span class="tag">IABS 1-2</span>
    <h2>Information About Brokerage Services</h2>
    <p>The required notice disclosing brokerage relationships to the parties involved in the transaction.</p>
    <div class="handled"><b>How TxtAnOffer handles it:</b> Included with every generated contract, with your saved brokerage details filled in automatically from your profile.</div>
  </div>

  <div class="sources-box">
    <h3>Verify this yourself</h3>
    <p>The forms above are TREC's promulgated contract forms. The Commission's separate rules of conduct and licensing rules -- the ones that govern how brokers and agents operate, not the forms themselves -- live in Texas Administrative Code Title 22, Part 23. Neither TREC's own courtesy copy nor this page is the official legal text; both are conveniences.</p>
    <ul>
      <li><a href="https://www.trec.texas.gov/agency-information/rules-and-laws/trec-rules" target="_blank" rel="noopener">TREC Rules</a> -- the Commission's own courtesy summary of Chapters 531 (ethics/conduct), 533 (procedure), 534 (administration), and 535 (licensure).</li>
      <li><a href="https://texreg.sos.state.tx.us/public/readtac$ext.ViewTAC?tac_view=3&amp;ti=22&amp;pt=23" target="_blank" rel="noopener">Texas Administrative Code, Title 22, Part 23</a> -- the Texas Secretary of State's official rule text, the version that actually controls.</li>
    </ul>
  </div>

  <div class="disclaimer-box">
    <strong>This page is for general information only -- it is not legal advice and is not a substitute for advice from a licensed Texas real estate attorney.</strong> TxtAnOffer is an independent, third-party tool and is NOT affiliated with, endorsed by, or partnered with the Texas Real Estate Commission (TREC). "TREC" and the form numbers referenced above are designations of the Texas Real Estate Commission. We use publicly available TREC promulgated forms as templates; if TREC revises or replaces a form, there may be a delay before this page and the Service are updated. You, the licensed agent, are responsible for independently confirming that the form version used is current and appropriate for your transaction. See our <a href="/terms" style="color:var(--text);text-decoration:underline;">Terms of Service</a> for the full disclaimer.
  </div>

  <div class="cta-row">
    <p>Text your offer terms and get back a contract built on the current form -- every time.</p>
    <a href="/">Try it free, no card needed &rarr;</a>
  </div>

  <p class="foot">Questions about a specific form change? Email <a href="mailto:support@txtanoffer.com">support@txtanoffer.com</a>.<br><a href="/">&larr; Back to home</a> &middot; <a href="/faq">FAQ</a> &middot; <a href="/terms">Terms</a></p>
</div>
</body>
</html>"""
    return html.replace("__TREC_FORM_DATE__", TREC_FORM_CURRENT_AS_OF)


@app.route("/about")
def about():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>About — TxtAnOffer</title>
<meta name="description" content="TxtAnOffer was built after real conversations with Texas agents about the 45 minutes lost filling out TREC paperwork. Here's why it exists.">
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
  :root {
    --bg: #F5F5F7;
    --bg-card: #fff;
    --border: rgba(15,31,47,0.08);
    --text: #0f1f2f;
    --text-muted: #5a6b7a;
    --text-dim: #8a9aa9;
    --accent: #171717;
    --accent-light: #525252;
    --accent-dark: #000000;
    --accent-tint: #F0F0EE;
    --radius: 1.25rem;
    --transition: all 0.2s ease;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
    background:var(--bg); color:var(--text); line-height:1.6;
    -webkit-font-smoothing:antialiased; min-height:100vh;
  }
  a { color:inherit; text-decoration:none; }
  .nav {
    display:flex;align-items:center;justify-content:space-between;
    padding:1rem 2rem;position:sticky;top:0;
    background:rgba(255,255,255,0.85);backdrop-filter:blur(20px);
    -webkit-backdrop-filter:blur(20px);
    border-bottom:1px solid var(--border);z-index:100;
  }
  .nav-left {display:flex;align-items:center;gap:0.6rem;font-weight:700;font-size:1.1rem;letter-spacing:-0.02em;color:var(--text);}
  .nav-logo {width:34px;height:34px;border-radius:22%;overflow:hidden;}
  .nav-logo img {width:100%;height:100%;object-fit:contain;}
  .nav-links {display:flex;gap:2rem;font-size:0.875rem;font-weight:500;color:var(--text-muted);}
  .nav-links a {transition:var(--transition);}
  .nav-links a:hover {color:var(--text);}
  .nav-cta {
    background:var(--accent);color:#fff;padding:0.55rem 1.35rem;border-radius:9999px;
    font-size:0.875rem;font-weight:600;text-decoration:none;display:inline-block;
    transition:var(--transition);
  }
  .nav-cta:hover {transform:scale(1.05);box-shadow:0 0 24px rgba(0,0,0,0.25);}
  .nav-toggle { display: none; flex-direction: column; justify-content: center; gap: 5px; width: 34px; height: 34px; background: none; border: none; cursor: pointer; padding: 0; }
  .nav-toggle span { display: block; width: 100%; height: 2px; background: var(--text); border-radius: 2px; }
  .container {max-width:680px;margin:0 auto;padding:3.5rem 2rem 4rem;}
  .avatar-lg {
    width:64px;height:64px;border-radius:50%;overflow:hidden;margin-bottom:1.5rem;
    border:2px solid var(--border);
  }
  .avatar-lg img {width:100%;height:100%;object-fit:cover;}
  h1 {font-size:2.1rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.4rem;color:var(--text);}
  .kicker {font-size:0.9rem;color:var(--accent-dark);font-weight:600;margin-bottom:1.75rem;}
  .about-body p {font-size:0.95rem;color:var(--text-muted);margin-bottom:1.1rem;}
  .about-body strong {color:var(--text);font-weight:600;}
  h2 {font-size:1.2rem;font-weight:700;margin:2rem 0 0.9rem;color:var(--text);}
  ul {margin:0 0 1.1rem 1.2rem;color:var(--text-muted);font-size:0.95rem;}
  li {margin-bottom:0.4rem;}
  .signoff {
    margin-top:2.5rem;padding-top:1.75rem;border-top:1px solid var(--border);
    font-size:0.9rem;color:var(--text-muted);
  }
  .signoff strong {display:block;color:var(--text);font-size:1rem;margin-bottom:0.2rem;}
  .foot {text-align:center;margin-top:3rem;font-size:0.8rem;color:var(--text-dim);}
  .foot a {color:var(--accent-dark);}
  .foot a:hover {text-decoration:underline;}
  @media(max-width:600px) {
    .container {padding:2.5rem 1.25rem 3rem;}
    .nav-toggle { display: flex; }
    .nav-links {
      display: none; position: absolute; top: 100%; left: 0; right: 0;
      flex-direction: column; gap: 0; padding: 0.5rem 1.25rem 1.25rem;
      background: #fff; border-bottom: 1px solid rgba(15,31,47,0.08);
    }
    .nav-links.open { display: flex; }
    .nav-links a { padding: 0.75rem 0; border-bottom: 1px solid rgba(15,31,47,0.08); }
    .nav-links a:last-child { border-bottom: none; }
  }
</style>
</head>
<body>
<nav class="nav">
  <a href="/" class="nav-left">
    <div class="nav-logo"><img src="/static/logo.svg" alt="TxtAnOffer"></div>
    <span>TxtAnOffer</span>
  </a>
  <div class="nav-links" id="navLinks">
    <a href="/#how">How it works</a>
    <a href="/#trust">Accuracy</a>
    <a href="/pricing">Pricing</a>
    <a href="/demo">Demo</a>
    <a href="/playground">Parser Playground</a>
    <a href="/faq">FAQ</a>
    <a href="/about">About</a>
    <a href="/contact">Support</a>
    <a href="/login">Log In</a>
  </div>
  <a href="/signup" class="nav-cta">Start Free Trial</a>
  <button class="nav-toggle" id="navToggle" aria-label="Menu" aria-expanded="false"><span></span><span></span><span></span></button>
</nav>
<script>
(function(){
  var t=document.getElementById('navToggle'), l=document.getElementById('navLinks');
  if(!t||!l) return;
  t.addEventListener('click', function(){
    var open = l.classList.toggle('open');
    t.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  l.querySelectorAll('a').forEach(function(a){
    a.addEventListener('click', function(){ l.classList.remove('open'); t.setAttribute('aria-expanded','false'); });
  });
})();
</script>

<div class="container">
  <div class="avatar-lg"><img src="/static/logo.svg" alt="TxtAnOffer"></div>
  <h1>Built After Listening to Texas Agents</h1>
  <div class="kicker">The story behind TxtAnOffer</div>

  <div class="about-body">
    <p>Hi, I'm <strong>Phanel Jean Baptiste</strong>, the founder of TxtAnOffer.</p>

    <p>I'm not a real estate agent &mdash; I'm a software builder with a passion for solving real problems with simple tools. TxtAnOffer started after a conversation with a Texas REALTOR who walked me through what a bad day actually looks like: standing in a driveway or sitting in a parking lot, laptop open, manually filling 40+ fields on a TREC 20-19 while a buyer waits, because in Texas real estate the agent who gets their offer in first often gets the house.</p>

    <p>That 45 minutes costs deals. So I built a way to skip it.</p>

    <p>TxtAnOffer turns what used to take a laptop and 45 minutes into a text message and 10 seconds. Text the details from your phone. Get a filled PDF. Review it, then send it.</p>

    <h2>Why It's Built This Way</h2>
    <p>Every feature exists because agents told me it mattered, not because a spec sheet said a contract tool should have it:</p>
    <ul>
      <li><strong>SMS-first</strong> because most agents I talked to are in the field on their phone, not at a desk</li>
      <li><strong>Auto-filled TREC forms</strong> because a blank field is where mistakes happen</li>
      <li><strong>Email delivery</strong> because the listing agent needs the offer now, not after you're back at the office</li>
      <li><strong>Draft warnings</strong> because nothing should go out until the licensed agent sending it has actually reviewed it</li>
    </ul>

    <h2>The Mission</h2>
    <p>Give every Texas agent the tools to compete with the big teams. You don't need an admin, a laptop, or 45 minutes. You need your phone and 10 seconds.</p>
  </div>

  <div class="signoff">
    <strong>Phanel Jean Baptiste</strong>
    Founder, TxtAnOffer<br>
    <a href="/contact" style="color:var(--accent-dark);">Get in touch</a>
  </div>

  <p class="foot"><a href="/">&larr; Back to home</a> &middot; <a href="/faq">FAQ</a> &middot; <a href="/contact">Contact</a></p>
</div>
</body>
</html>"""
    return html


@app.route("/contact")
def contact():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Contact — TxtAnOffer</title>
<meta name="description" content="Get in touch with TxtAnOffer support by email or text.">
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
  :root {
    --bg: #F5F5F7;
    --bg-card: #fff;
    --border: rgba(15,31,47,0.08);
    --text: #0f1f2f;
    --text-muted: #5a6b7a;
    --text-dim: #8a9aa9;
    --accent: #171717;
    --accent-light: #525252;
    --accent-dark: #000000;
    --accent-tint: #F0F0EE;
    --radius: 1.25rem;
    --transition: all 0.2s ease;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
    background:var(--bg); color:var(--text); line-height:1.5;
    -webkit-font-smoothing:antialiased; min-height:100vh;
  }
  a { color:inherit; text-decoration:none; }
  .nav {
    display:flex;align-items:center;justify-content:space-between;
    padding:1rem 2rem;position:sticky;top:0;
    background:rgba(255,255,255,0.85);backdrop-filter:blur(20px);
    -webkit-backdrop-filter:blur(20px);
    border-bottom:1px solid var(--border);z-index:100;
  }
  .nav-left {display:flex;align-items:center;gap:0.6rem;font-weight:700;font-size:1.1rem;letter-spacing:-0.02em;color:var(--text);}
  .nav-logo {width:34px;height:34px;border-radius:22%;overflow:hidden;}
  .nav-logo img {width:100%;height:100%;object-fit:contain;}
  .nav-links {display:flex;gap:2rem;font-size:0.875rem;font-weight:500;color:var(--text-muted);}
  .nav-links a {transition:var(--transition);}
  .nav-links a:hover {color:var(--text);}
  .nav-cta {
    background:var(--accent);color:#fff;padding:0.55rem 1.35rem;border-radius:9999px;
    font-size:0.875rem;font-weight:600;text-decoration:none;display:inline-block;
    transition:var(--transition);
  }
  .nav-cta:hover {transform:scale(1.05);box-shadow:0 0 24px rgba(0,0,0,0.25);}
  .nav-toggle { display: none; flex-direction: column; justify-content: center; gap: 5px; width: 34px; height: 34px; background: none; border: none; cursor: pointer; padding: 0; }
  .nav-toggle span { display: block; width: 100%; height: 2px; background: var(--text); border-radius: 2px; }
  .container {max-width:560px;margin:0 auto;padding:3.5rem 2rem 4rem;}
  .page-header {margin-bottom:2rem;}
  .page-header h1 {font-size:2rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.4rem;color:var(--text);}
  .page-header p {font-size:0.9rem;color:var(--text-muted);}
  .contact-card {
    background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
    padding:1.75rem;margin-bottom:1rem;display:flex;align-items:center;gap:1rem;
    transition:var(--transition);box-shadow:0 1px 3px rgba(15,31,47,0.05);
  }
  a.contact-card:hover {border-color:rgba(0,0,0,0.3);transform:translateY(-1px);}
  .contact-icon {
    width:44px;height:44px;border-radius:50%;background:var(--accent-tint);
    display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:1.2rem;
  }
  .contact-label {font-size:0.75rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.04em;margin-bottom:0.15rem;}
  .contact-value {font-size:1rem;font-weight:600;color:var(--text);}
  .foot {text-align:center;margin-top:2rem;font-size:0.8rem;color:var(--text-dim);}
  .foot a {color:var(--accent-dark);}
  .foot a:hover {text-decoration:underline;}
  @media(max-width:600px) {
    .container {padding:2.5rem 1.25rem 3rem;}
    .nav-toggle { display: flex; }
    .nav-links {
      display: none; position: absolute; top: 100%; left: 0; right: 0;
      flex-direction: column; gap: 0; padding: 0.5rem 1.25rem 1.25rem;
      background: #fff; border-bottom: 1px solid rgba(15,31,47,0.08);
    }
    .nav-links.open { display: flex; }
    .nav-links a { padding: 0.75rem 0; border-bottom: 1px solid rgba(15,31,47,0.08); }
    .nav-links a:last-child { border-bottom: none; }
  }
</style>
</head>
<body>
<nav class="nav">
  <a href="/" class="nav-left">
    <div class="nav-logo"><img src="/static/logo.svg" alt="TxtAnOffer"></div>
    <span>TxtAnOffer</span>
  </a>
  <div class="nav-links" id="navLinks">
    <a href="/#how">How it works</a>
    <a href="/#trust">Accuracy</a>
    <a href="/pricing">Pricing</a>
    <a href="/demo">Demo</a>
    <a href="/playground">Parser Playground</a>
    <a href="/faq">FAQ</a>
    <a href="/about">About</a>
    <a href="/contact">Support</a>
    <a href="/login">Log In</a>
  </div>
  <a href="/signup" class="nav-cta">Start Free Trial</a>
  <button class="nav-toggle" id="navToggle" aria-label="Menu" aria-expanded="false"><span></span><span></span><span></span></button>
</nav>
<script>
(function(){
  var t=document.getElementById('navToggle'), l=document.getElementById('navLinks');
  if(!t||!l) return;
  t.addEventListener('click', function(){
    var open = l.classList.toggle('open');
    t.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  l.querySelectorAll('a').forEach(function(a){
    a.addEventListener('click', function(){ l.classList.remove('open'); t.setAttribute('aria-expanded','false'); });
  });
})();
</script>

<div class="container">
  <div class="page-header">
    <h1>Get in Touch</h1>
    <p>Questions, feedback, or need a hand? Reach out directly &mdash; a real person reads every message.</p>
  </div>

  <a class="contact-card" href="mailto:support@txtanoffer.com">
    <div class="contact-icon">&#9993;</div>
    <div>
      <div class="contact-label">Email</div>
      <div class="contact-value">support@txtanoffer.com</div>
    </div>
  </a>

  <a class="contact-card" href="sms:+18338970333">
    <div class="contact-icon">&#128241;</div>
    <div>
      <div class="contact-label">Text</div>
      <div class="contact-value">+1 (833) 897-0333</div>
    </div>
  </a>

  <p class="foot">Looking for answers first? Check the <a href="/faq">FAQ</a>.<br><a href="/">&larr; Back to home</a></p>
</div>
</body>
</html>"""
    return html


@app.route("/profile", methods=["GET", "POST"])
def profile():
    # request.values (not request.args) so the signature still verifies on
    # POST -- the form below carries phone/expires/sig forward as hidden
    # fields rather than a query string, since <form action> drops it.
    phone = request.values.get("phone", "").strip()
    expires = request.values.get("expires", "")
    sig = request.values.get("sig", "")

    if not verify_dashboard_signature(phone, expires, sig):
        abort(403)

    saved = False
    error = ""

    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        if not phone:
            error = "Phone number is required."
        else:
            save_agent_profile(phone, {
                "name": request.form.get("name", "").strip().title(),
                "license": request.form.get("license", "").strip(),
                "phone": phone,
                "email": request.form.get("email", "").strip(),
                "brokerage": request.form.get("brokerage", "").strip(),
                "business_address": request.form.get("business_address", "").strip(),
                "title_company": request.form.get("title_company", "").strip(),
                "title_company_address": request.form.get("title_company_address", "").strip(),
                "default_earnest_pct": float(request.form.get("earnest_pct", "1") or "1") / 100,
                "default_option_fee": int(float(request.form.get("option_fee", "250") or "250")),
            })
            saved = True

    existing = get_agent_profile(phone) if phone else {}

    # Preview of the attribution card recipients see on /thread pages --
    # answers "why don't I see my card" directly, right where an agent would
    # look for it, instead of leaving them to guess.
    preview_name = (existing.get("name") or "").strip()
    if phone and has_professional_access(phone):
        if preview_name:
            preview_brokerage = (existing.get("brokerage") or "").strip()
            preview_license = (existing.get("license") or "").strip()
            preview_meta_parts = [p for p in [preview_brokerage, f"License #{preview_license}" if preview_license else ""] if p]
            preview_meta = " &middot; ".join(preview_meta_parts)
            preview_initials = "".join(p[0].upper() for p in preview_name.split()[:2]) or "TX"
            preview_block = f"""
  <div class="preview-label">How this appears to recipients</div>
  <div class="agent-card">
    <div class="agent-avatar">{preview_initials}</div>
    <div>
      <div class="agent-name">{preview_name}</div>
      {f'<div class="agent-meta">{preview_meta}</div>' if preview_meta else ''}
    </div>
  </div>"""
        else:
            preview_block = """
  <div class="preview-hint">Fill in your name below and save to see your agent card &mdash; it appears at the top of every offer page a listing agent opens.</div>"""
    else:
        preview_block = """
  <div class="preview-hint">Agent branding (the card recipients see with your name, brokerage, and license) is a <a href="/pricing">Professional-plan</a> feature. Upgrade to have it appear on your offer pages.</div>"""

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Agent Profile — TxtAnOffer</title>
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
  :root{{
    --bg: #F5F5F7;
    --bg-card: #fff;
    --border: rgba(15,31,47,0.08);
    --border-hover: rgba(23,23,23,0.35);
    --text: #0f1f2f;
    --text-muted: #5a6b7a;
    --text-dim: #8a9aa9;
    --accent: #171717;
    --accent-light: #525252;
    --accent-dark: #000000;
    --accent-tint: #F0F0EE;
    --radius: 1.25rem;
    --radius-sm: 0.85rem;
    --transition: all 0.2s ease;
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
    background:var(--bg);
    color:var(--text);
    line-height:1.5;
    -webkit-font-smoothing:antialiased;
    min-height:100vh;
  }}
  a {{ color:inherit; text-decoration:none; }}

  .nav {{
    display:flex;align-items:center;justify-content:space-between;
    padding:1rem 2rem;position:sticky;top:0;
    background:rgba(255,255,255,0.85);backdrop-filter:blur(20px);
    -webkit-backdrop-filter:blur(20px);
    border-bottom:1px solid var(--border);z-index:100;
  }}
  .nav-left {{display:flex;align-items:center;gap:0.6rem;font-weight:700;font-size:1.1rem;letter-spacing:-0.02em;color:var(--text);}}
  .nav-logo {{width:34px;height:34px;border-radius:22%;overflow:hidden;}}
  .nav-logo img {{width:100%;height:100%;object-fit:contain;}}
  .nav-links {{display:flex;gap:2rem;font-size:0.875rem;font-weight:500;color:var(--text-muted);}}
  .nav-links a {{transition:var(--transition);}}
  .nav-links a:hover {{color:var(--text);}}
  .nav-cta {{
    background:var(--accent);color:#fff;padding:0.55rem 1.35rem;border-radius:9999px;
    font-size:0.875rem;font-weight:600;text-decoration:none;display:inline-block;
    transition:var(--transition);
  }}
  .nav-cta:hover {{transform:scale(1.05);box-shadow:0 0 24px rgba(23,23,23,0.3);}}
  .nav-toggle {{ display: none; flex-direction: column; justify-content: center; gap: 5px; width: 34px; height: 34px; background: none; border: none; cursor: pointer; padding: 0; }}
  .nav-toggle span {{ display: block; width: 100%; height: 2px; background: var(--text); border-radius: 2px; }}

  .container {{max-width:520px;margin:0 auto;padding:3rem 1.5rem 4rem;}}
  .page-header {{margin-bottom:2rem;}}
  .page-header h1 {{font-size:1.75rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.25rem;color:var(--text);}}
  .page-header p {{color:var(--text-muted);font-size:0.9rem;}}

  .form-card {{
    background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
    padding:2rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);
  }}
  .preview-label {{
    font-size:0.7rem;font-weight:700;color:var(--text-dim);
    text-transform:uppercase;letter-spacing:0.07em;margin-bottom:0.6rem;
  }}
  .agent-card{{display:flex;align-items:center;gap:0.85rem;background:var(--bg-card);border:1px solid var(--border);
  border-radius:var(--radius-sm);padding:0.9rem 1.1rem;margin-bottom:1.5rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);}}
  .agent-avatar{{width:42px;height:42px;border-radius:50%;background:var(--accent-tint);color:var(--accent-dark);
  display:flex;align-items:center;justify-content:center;font-weight:700;font-size:0.9rem;flex-shrink:0;}}
  .agent-name{{font-size:0.9rem;font-weight:700;color:var(--text);}}
  .agent-meta{{font-size:0.78rem;color:var(--text-dim);margin-top:0.1rem;}}
  .preview-hint{{font-size:0.82rem;color:var(--text-dim);background:var(--bg-card);border:1px solid var(--border);
  border-radius:var(--radius-sm);padding:0.9rem 1.1rem;margin-bottom:1.5rem;line-height:1.5;}}
  .preview-hint a{{color:var(--accent-dark);font-weight:600;}}
  .field-label {{
    font-size:0.7rem;font-weight:700;color:var(--text-dim);
    text-transform:uppercase;letter-spacing:0.07em;margin-bottom:0.5rem;display:block;
    margin-top:1.25rem;
  }}
  .field-label:first-child {{margin-top:0;}}
  .form-card input {{
    width:100%;background:#fff;border:1px solid rgba(15,31,47,0.14);
    border-radius:var(--radius-sm);padding:0.75rem 1rem;color:var(--text);
    font-size:0.9rem;font-family:inherit;outline:none;transition:var(--transition);
  }}
  .form-card input:focus {{border-color:var(--accent);box-shadow:0 0 0 3px rgba(23,23,23,0.15);}}
  .form-card input::placeholder {{color:#b8c2ca;}}
  .row {{display:flex;gap:0.75rem;}}
  .row > div {{flex:1;}}
  .form-card button {{
    width:100%;margin-top:1.5rem;
    background:linear-gradient(135deg,var(--accent),#000000);color:#fff;border:none;
    border-radius:var(--radius-sm);padding:0.85rem;font-weight:600;font-size:0.95rem;
    font-family:inherit;cursor:pointer;transition:var(--transition);
  }}
  .form-card button:hover {{transform:translateY(-2px);box-shadow:0 8px 24px rgba(23,23,23,0.25);}}
  .success {{
    margin-top:1rem;padding:0.85rem 1rem;
    background:var(--accent-tint);border:1px solid rgba(23,23,23,0.25);
    border-radius:var(--radius-sm);font-size:0.85rem;color:var(--accent-dark);text-align:center;
  }}
  .error {{
    margin-top:1rem;padding:0.85rem 1rem;
    background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.2);
    border-radius:var(--radius-sm);font-size:0.85rem;color:#dc2626;text-align:center;
  }}
  .foot {{text-align:center;margin-top:1.5rem;font-size:0.8rem;color:var(--text-dim);}}
  .foot a {{color:var(--accent-dark);}}
  .foot a:hover {{text-decoration:underline;}}

  @media(max-width:600px){{
    .container {{padding:2rem 1rem 3rem;}}
    .form-card {{padding:1.5rem 1.25rem;}}
    .nav-toggle {{ display: flex; }}
    .nav-links {{
      display: none; position: absolute; top: 100%; left: 0; right: 0;
      flex-direction: column; gap: 0; padding: 0.5rem 1.25rem 1.25rem;
      background: #fff; border-bottom: 1px solid rgba(15,31,47,0.08);
    }}
    .nav-links.open {{ display: flex; }}
    .nav-links a {{ padding: 0.75rem 0; border-bottom: 1px solid rgba(15,31,47,0.08); }}
    .nav-links a:last-child {{ border-bottom: none; }}
    .row {{flex-direction:column;gap:0;}}
  }}
</style>
</head>
<body>
<nav class="nav">
  <a href="/" class="nav-left">
    <div class="nav-logo"><img src="/static/logo.svg" alt="TxtAnOffer"></div>
    <span>TxtAnOffer</span>
  </a>
  <div class="nav-links" id="navLinks">
    <a href="/">Home</a>
    <a href="/demo">Demo</a>
    <a href="/pricing">Pricing</a>
  </div>
  <a href="/signup" class="nav-cta">Start Free Trial</a>
  <button class="nav-toggle" id="navToggle" aria-label="Menu" aria-expanded="false"><span></span><span></span><span></span></button>
</nav>
<script>
(function(){{
  var t=document.getElementById('navToggle'), l=document.getElementById('navLinks');
  if(!t||!l) return;
  t.addEventListener('click', function(){{
    var open = l.classList.toggle('open');
    t.setAttribute('aria-expanded', open ? 'true' : 'false');
  }});
  l.querySelectorAll('a').forEach(function(a){{
    a.addEventListener('click', function(){{ l.classList.remove('open'); t.setAttribute('aria-expanded','false'); }});
  }});
}})();
</script>

<div class="container">
  <div class="page-header">
    <h1>Agent Profile</h1>
    <p>Your info auto-fills the cover page on every offer you generate.</p>
  </div>
{preview_block}

  <div class="form-card">
    <form method="POST" action="/profile">
      <input type="hidden" name="expires" value="{expires}">
      <input type="hidden" name="sig" value="{sig}">
      <label class="field-label">Phone number (used for SMS offers)</label>
      <input type="text" name="phone" placeholder="+15125551234" value="{phone or existing.get('phone', '')}" required>

      <label class="field-label">Full name</label>
      <input type="text" name="name" placeholder="Jane Smith" value="{existing.get('name', '')}">

      <label class="field-label">TREC license number</label>
      <input type="text" name="license" placeholder="0123456" value="{existing.get('license', '')}">

      <label class="field-label">Email</label>
      <input type="email" name="email" placeholder="jane@realty.com" value="{existing.get('email', '')}">

      <label class="field-label">Brokerage</label>
      <input type="text" name="brokerage" placeholder="Keller Williams" value="{existing.get('brokerage', '')}">

      <label class="field-label">Business address</label>
      <input type="text" name="business_address" placeholder="123 Main St, Austin, TX 78701" value="{existing.get('business_address', '')}">

      <label class="field-label">Title company</label>
      <input type="text" name="title_company" placeholder="Texas Title Co." value="{existing.get('title_company', '')}">

      <label class="field-label">Title company address</label>
      <input type="text" name="title_company_address" placeholder="456 Congress Ave, Austin, TX 78701" value="{existing.get('title_company_address', '')}">

      <div class="row">
        <div>
          <label class="field-label">Default earnest %</label>
          <input type="number" name="earnest_pct" step="0.1" min="0.1" max="10" value="{existing.get('default_earnest_pct', 0.01) * 100:.1f}">
        </div>
        <div>
          <label class="field-label">Default option fee $</label>
          <input type="number" name="option_fee" min="0" max="5000" value="{existing.get('default_option_fee', 250)}">
        </div>
      </div>

      <button type="submit">Save Profile</button>
    </form>
    {'<div class="success">Profile saved! Your info will appear on all future offers.</div>' if saved else ''}
    {'<div class="error">' + error + '</div>' if error else ''}
  </div>
  <div class="foot"><a href="/demo">&larr; Back to demo</a> &middot; <a href="/dashboard?phone={_urlquote(phone, safe='')}&expires={expires}&sig={sig}">Dashboard</a></div>
</div>
</body>
</html>
"""


@app.route("/review/<path:filename>")
def review_offer(filename):
    if ".." in filename or filename.startswith("/"):
        abort(400)
    expires = request.args.get("expires")
    sig = request.args.get("sig")
    if not verify_pdf_signature(filename, expires, sig):
        abort(403)

    offer = get_offer_by_filename(filename)
    if not offer or not offer["price"]:
        return redirect(f"/offers/{filename}?expires={expires}&sig={sig}")
    address = offer["address"]
    price = offer["price"]
    down_pct = offer["down_pct"]
    close_days = offer["close_days"]
    down_amt = int(price * down_pct) if price else 0
    loan_amt = price - down_amt if price else 0
    mls = offer.get("mls", {})
    email_sent_at = offer.get("email_sent_at") or ""
    email_sent_to = offer.get("email_sent_to") or ""

    pdf_path_on_disk = os.path.join(OUTPUT_DIR, filename)
    validation = validate_offer_pdf(pdf_path_on_disk, {
        "price": price, "down_payment_amount": down_amt, "loan_amount": loan_amt,
        "close_days": close_days, "created_at": offer.get("created_at"),
        "financing_type_specified": bool(offer.get("financing_type")),
    }) if os.path.exists(pdf_path_on_disk) else {"ok": False, "blocking": ["PDF file not found on server"], "warnings": []}
    # DocuSign has no "fill in by hand" escape hatch (unlike Email/Download,
    # where the agent can still type the name in before sending) -- a blank
    # buyer/seller legal name must disable this button too, even though
    # it's only a warning for the other two send paths. Matches the
    # server-side check in api_docusign().
    docusign_blocking = validation["blocking"] or [w for w in validation["warnings"] if "legal name is blank" in w]

    pdf_url = f"/offers/{filename}?expires={expires}&sig={sig}"

    from datetime import timedelta
    # Anchor to the offer's actual creation time, not "now" -- this page can
    # be opened any time after generation, and the closing date is fixed at
    # generation time (baked into the PDF then). Recomputing from "now" here
    # made the summary card silently drift a day later for every day that
    # passes before the agent opens this link, disagreeing with the PDF.
    try:
        created_dt = datetime.fromisoformat(offer["created_at"])
    except (KeyError, TypeError, ValueError):
        created_dt = datetime.now()
    close_date = (created_dt + timedelta(days=close_days)).strftime("%B %d, %Y") if close_days else ""

    sent_date = ""
    if email_sent_at:
        try:
            sent_date = datetime.fromisoformat(email_sent_at).strftime("%B %d, %Y at %I:%M %p")
        except ValueError:
            sent_date = ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Offer Review — {address}</title>
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
:root{{--bg:#F5F5F7;--bg-card:#fff;--border:rgba(15,31,47,0.08);
--text:#0f1f2f;--text-muted:#5a6b7a;--text-dim:#8a9aa9;--accent:#171717;--accent-light:#525252;
--accent-dark:#000000;--accent-tint:#F0F0EE;--radius:1.25rem;--radius-sm:0.85rem;}}
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;
-webkit-font-smoothing:antialiased;}}
.top-bar{{background:var(--accent-tint);border-bottom:1px solid rgba(23,23,23,0.2);
padding:0.6rem 1.5rem;text-align:center;font-size:0.8rem;color:var(--accent-dark);font-weight:600;}}
.container{{max-width:600px;margin:0 auto;padding:1.5rem 1rem;}}
.address-card{{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
padding:1.5rem;text-align:center;margin-bottom:1rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);}}
.address-card h1{{font-size:1.25rem;font-weight:700;margin-bottom:0.25rem;color:var(--text);}}
.address-card .meta{{color:var(--text-dim);font-size:0.8rem;}}
.stats{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:0.5rem;margin-bottom:1rem;}}
.stat{{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-sm);
padding:0.85rem 0.5rem;text-align:center;box-shadow:0 1px 3px rgba(15,31,47,0.05);}}
.stat-label{{font-size:0.65rem;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;
color:var(--text-dim);margin-bottom:0.2rem;}}
.stat-value{{font-size:1rem;font-weight:700;color:var(--text);}}
.stat-value.accent{{color:var(--accent-dark);}}
.actions{{display:flex;flex-direction:column;gap:0.6rem;margin-bottom:1.25rem;}}
.btn{{display:flex;align-items:center;justify-content:center;gap:0.5rem;padding:0.9rem 1rem;
border-radius:var(--radius-sm);font-family:inherit;font-size:0.9rem;font-weight:600;
text-decoration:none;border:none;cursor:pointer;transition:all 0.2s;}}
.btn-primary{{background:linear-gradient(135deg,var(--accent),#000000);color:#fff;}}
.btn-primary:hover{{transform:translateY(-1px);box-shadow:0 6px 20px rgba(23,23,23,0.25);}}
.btn-secondary{{background:var(--bg-card);color:var(--text);border:1px solid var(--border);}}
.btn-secondary:hover{{border-color:var(--accent);}}
.btn-outline{{background:transparent;color:var(--text-muted);border:1px solid var(--border);}}
.btn-outline:hover{{border-color:var(--accent);color:var(--accent-dark);}}
.pdf-frame{{width:100%;height:70vh;border:1px solid var(--border);border-radius:var(--radius-sm);
background:#f1f5f9;}}
.pdf-preview-card{{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
padding:0.75rem;margin-bottom:1rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);text-align:center;}}
.pdf-preview-card a{{display:block;}}
.pdf-preview-img{{width:100%;display:block;border-radius:var(--radius-sm);border:1px solid var(--border);}}
.pdf-preview-caption{{font-size:0.78rem;color:var(--text-dim);margin-top:0.6rem;}}
.pdf-preview-caption a{{display:inline;color:var(--accent-dark);font-weight:600;text-decoration:none;}}
.pdf-preview-caption a:hover{{text-decoration:underline;}}
.email-form{{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
padding:1.25rem;margin-bottom:1rem;display:none;}}
.email-form.show{{display:block;}}
.email-form label{{font-size:0.8rem;font-weight:600;color:var(--text-dim);display:block;margin-bottom:0.4rem;}}
.email-form input{{width:100%;padding:0.7rem;background:#fff;border:1px solid rgba(15,31,47,0.14);
border-radius:var(--radius-sm);color:var(--text);font-family:inherit;font-size:0.9rem;outline:none;
margin-bottom:0.75rem;}}
.email-form input:focus{{border-color:var(--accent);}}
.email-status{{font-size:0.85rem;padding:0.5rem;border-radius:var(--radius-sm);margin-top:0.5rem;display:none;}}
.email-status.success{{display:block;background:var(--accent-tint);color:var(--accent-dark);}}
.email-status.error{{display:block;background:rgba(239,68,68,0.08);color:#dc2626;}}
.sent-banner{{background:var(--accent-tint);border:1px solid rgba(23,23,23,0.25);color:var(--accent-dark);
border-radius:var(--radius-sm);padding:0.75rem 1rem;text-align:center;font-size:0.85rem;margin-bottom:1rem;}}
.sent-banner strong{{color:var(--text);}}
.disclaimer{{font-size:0.75rem;color:var(--text-dim);text-align:center;padding:1rem;
border-top:1px solid var(--border);margin-top:1rem;}}
.btn:disabled{{opacity:0.45;cursor:not-allowed;}}
.btn:disabled:hover{{transform:none;box-shadow:none;}}
.qa-blocking, .qa-warnings{{border-radius:var(--radius-sm);padding:0.85rem 1rem;margin-bottom:0.85rem;font-size:0.85rem;}}
.qa-blocking{{background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.25);color:#dc2626;}}
.qa-warnings{{background:rgba(245,158,11,0.10);border:1px solid rgba(245,158,11,0.3);color:#b45309;}}
.qa-blocking strong, .qa-warnings strong{{display:block;margin-bottom:0.4rem;color:var(--text);}}
.qa-blocking ul, .qa-warnings ul{{margin:0;padding-left:1.1rem;}}
.qa-blocking li, .qa-warnings li{{margin-bottom:0.2rem;}}
@media(max-width:400px){{
.stats{{grid-template-columns:1fr 1fr;}}
.stat:last-child{{grid-column:span 2;}}
}}
</style>
</head>
<body>
<div class="top-bar">TREC 20-19 (mandatory as of {TREC_FORM_CURRENT_AS_OF}) — Review before signing</div>
<div class="container">
<div class="address-card">
<h1>{address}</h1>
<div class="meta">TREC One to Four Family Residential Contract</div>
</div>

<div class="stats">
<div class="stat"><div class="stat-label">Price</div><div class="stat-value accent">${price:,}</div></div>
<div class="stat"><div class="stat-label">Down</div><div class="stat-value">{down_pct*100:.0f}% (${down_amt:,})</div></div>
<div class="stat"><div class="stat-label">Close</div><div class="stat-value">{close_date}</div></div>
</div>

{'<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-sm);padding:0.6rem;text-align:center;margin-bottom:1rem;color:var(--text-muted);font-size:0.8rem;">' + ' &middot; '.join([x for x in [f"{mls.get('bed')} Bed" if mls.get('bed') else '', f"{mls.get('bath')} Bath" if mls.get('bath') else '', f"{mls.get('sqft'):,} Sqft" if mls.get('sqft') else '', f"Built {mls.get('year_built')}" if mls.get('year_built') else ''] if x]) + '</div>' if any(mls.get(k) for k in ('bed','bath','sqft')) else ''}

{'<div class="qa-blocking"><strong>Can&rsquo;t send yet &mdash; fix before emailing:</strong><ul>' + ''.join(f'<li>{b}</li>' for b in validation['blocking']) + '</ul></div>' if validation['blocking'] else ''}
{'<div class="qa-warnings"><strong>Heads up before sending:</strong><ul>' + ''.join(f'<li>{w}</li>' for w in validation['warnings']) + '</ul></div>' if validation['warnings'] else ''}

{'<div class="sent-banner" id="sent-banner">&#9989; Sent to <strong>' + email_sent_to + '</strong> on ' + sent_date + '</div>' if email_sent_at else ''}

<div class="pdf-preview-card">
<a href="{pdf_url}" target="_blank"><img src="/offers/{filename}/preview.png?expires={expires}&sig={sig}" alt="Offer PDF preview" class="pdf-preview-img" loading="lazy"></a>
<div class="pdf-preview-caption">Page 1 &middot; <a href="{pdf_url}" target="_blank">View all pages</a></div>
</div>

<div class="actions">
<button class="btn btn-primary" id="email-toggle"{' disabled' if validation['blocking'] else ''}>{'Resend to Listing Agent' if email_sent_at else 'Email to Listing Agent'}</button>
<a href="{pdf_url}" class="btn btn-secondary" target="_blank">Open PDF</a>
<a href="{pdf_url}" class="btn btn-outline" download="{filename}">Download PDF</a>
</div>

<div class="actions">
<button class="btn btn-secondary" id="docusign-toggle"{' disabled' if docusign_blocking else ''}>Send to DocuSign</button>
<button class="btn btn-secondary" id="webhook-toggle">Webhook / Zapier</button>
</div>

<div class="email-form" id="email-form">
<label>Listing agent's email</label>
<input type="email" id="email-to" placeholder="agent@example.com" value="{email_sent_to}">
<button class="btn btn-primary" id="send-email-btn" style="width:100%;">Send Offer PDF</button>
<div class="email-status" id="email-status"></div>
</div>

<div class="email-form" id="docusign-form">
<label>Listing agent's name</label>
<input type="text" id="ds-name" placeholder="Jane Smith">
<label>Listing agent's email</label>
<input type="email" id="ds-email" placeholder="agent@example.com">
<button class="btn btn-primary" id="send-docusign-btn" style="width:100%;">Send via DocuSign</button>
<div class="email-status" id="docusign-status"></div>
</div>

<div class="email-form" id="webhook-form">
<label>Webhook URL (Zapier or any endpoint)</label>
<input type="url" id="wh-url" placeholder="https://hooks.zapier.com/...">
<button class="btn btn-primary" id="save-webhook-btn" style="width:100%;">Save Webhook</button>
<div class="email-status" id="webhook-status"></div>
</div>

<iframe src="{pdf_url}" class="pdf-frame" title="Offer PDF"></iframe>

<div class="disclaimer">
This is a draft generated by TxtAnOffer. Agent must review all fields before signing or presenting.
Not affiliated with TREC. &middot; <a href="/" style="color:var(--accent-dark);">txtanoffer.com</a>
</div>
</div>

<script>
(function(){{
var toggle=document.getElementById('email-toggle'),
    form=document.getElementById('email-form'),
    sendBtn=document.getElementById('send-email-btn'),
    statusEl=document.getElementById('email-status'),
    emailInput=document.getElementById('email-to');

toggle.addEventListener('click',function(){{
  form.classList.toggle('show');
  if(form.classList.contains('show'))emailInput.focus();
}});

sendBtn.addEventListener('click',function(){{
  var email=emailInput.value.trim();
  if(!email)return;
  statusEl.className='email-status';statusEl.style.display='none';
  sendBtn.textContent='Sending...';sendBtn.disabled=true;
  fetch('/api/send-email',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{to_email:email,pdf_filename:'{filename}',parsed:{{address:'{address}',price:{price}}},expires:'{expires}',sig:'{sig}'}})
  }}).then(function(r){{return r.json();}}).then(function(d){{
    if(d.success){{
      statusEl.textContent='Sent! The listing agent will receive the PDF.';statusEl.className='email-status success';
      sendBtn.textContent='✓ Sent';sendBtn.disabled=true;
      toggle.textContent='Resend to Listing Agent';
      var banner=document.getElementById('sent-banner');
      var bannerHtml='&#9989; Sent to <strong>'+email+'</strong> just now';
      if(banner){{banner.innerHTML=bannerHtml;}}
      else{{
        banner=document.createElement('div');banner.className='sent-banner';banner.id='sent-banner';
        banner.innerHTML=bannerHtml;
        var actionsDiv=document.querySelector('.actions');
        actionsDiv.parentNode.insertBefore(banner,actionsDiv);
      }}
      setTimeout(function(){{sendBtn.textContent='Send Offer PDF';sendBtn.disabled=false;}},2500);
    }}else{{
      statusEl.textContent=d.error||'Failed to send.';statusEl.className='email-status error';
      sendBtn.textContent='Send Offer PDF';sendBtn.disabled=false;
    }}
  }}).catch(function(){{
    statusEl.textContent='Network error. Try again.';statusEl.className='email-status error';
    sendBtn.textContent='Send Offer PDF';sendBtn.disabled=false;
  }});
}});

emailInput.addEventListener('keydown',function(e){{if(e.key==='Enter')sendBtn.click();}});

var dsToggle=document.getElementById('docusign-toggle'),
    dsForm=document.getElementById('docusign-form'),
    dsBtn=document.getElementById('send-docusign-btn'),
    dsStatus=document.getElementById('docusign-status'),
    dsName=document.getElementById('ds-name'),
    dsEmail=document.getElementById('ds-email');

dsToggle.addEventListener('click',function(){{
  dsForm.classList.toggle('show');
  if(dsForm.classList.contains('show'))dsName.focus();
}});

dsBtn.addEventListener('click',function(){{
  var name=dsName.value.trim(),email=dsEmail.value.trim();
  if(!name||!email){{dsStatus.textContent='Name and email required';dsStatus.className='email-status error';return;}}
  dsStatus.className='email-status';dsStatus.style.display='none';
  dsBtn.textContent='Sending...';dsBtn.disabled=true;
  fetch('/api/docusign',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{pdf_filename:'{filename}',signer_email:email,signer_name:name,parsed:{{address:'{address}'}},expires:'{expires}',sig:'{sig}'}})
  }}).then(function(r){{return r.json();}}).then(function(d){{
    if(d.success){{
      dsStatus.textContent='Sent! Envelope: '+d.envelope_id;dsStatus.className='email-status success';
      dsBtn.textContent='✓ Sent';
    }}else{{
      dsStatus.textContent=d.error||'Failed to send.';dsStatus.className='email-status error';
      dsBtn.textContent='Send via DocuSign';dsBtn.disabled=false;
    }}
  }}).catch(function(){{
    dsStatus.textContent='Network error. Try again.';dsStatus.className='email-status error';
    dsBtn.textContent='Send via DocuSign';dsBtn.disabled=false;
  }});
}});

var whToggle=document.getElementById('webhook-toggle'),
    whForm=document.getElementById('webhook-form'),
    whBtn=document.getElementById('save-webhook-btn'),
    whStatus=document.getElementById('webhook-status'),
    whUrl=document.getElementById('wh-url');

whToggle.addEventListener('click',function(){{
  whForm.classList.toggle('show');
  if(whForm.classList.contains('show'))whUrl.focus();
}});

whBtn.addEventListener('click',function(){{
  var url=whUrl.value.trim();
  if(!url){{whStatus.textContent='Enter a webhook URL';whStatus.className='email-status error';return;}}
  whStatus.className='email-status';whStatus.style.display='none';
  whBtn.textContent='Saving...';whBtn.disabled=true;
  fetch('/api/webhook',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{source_id:'{offer["phone"]}',url:url,filename:'{filename}',expires:'{expires}',sig:'{sig}'}})
  }}).then(function(r){{return r.json();}}).then(function(d){{
    if(d.success){{
      whStatus.textContent='Webhook saved! Future offers will POST here.';whStatus.className='email-status success';
      whBtn.textContent='✓ Saved';
    }}else{{
      whStatus.textContent=d.error||'Failed to save.';whStatus.className='email-status error';
      whBtn.textContent='Save Webhook';whBtn.disabled=false;
    }}
  }}).catch(function(){{
    whStatus.textContent='Network error. Try again.';whStatus.className='email-status error';
    whBtn.textContent='Save Webhook';whBtn.disabled=false;
  }});
}});
}})();
</script>
</body>
</html>"""


THREAD_EXPIRED_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Offer Thread - TxtAnOffer</title>
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet"></noscript>
<style>
body{font-family:'Inter',sans-serif;background:#F5F5F7;color:#0f1f2f;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px;}
.box{background:#fff;border-radius:1.25rem;padding:2.5rem;max-width:400px;text-align:center;
border:1px solid rgba(15,31,47,0.08);box-shadow:0 1px 2px rgba(15,31,47,0.04);}
h2{margin:0 0 0.75rem;font-size:1.35rem;font-weight:700;}
p{color:#5a6b7a;font-size:0.9rem;line-height:1.6;}
a{color:#000000;text-decoration:none;}
a:hover{text-decoration:underline;}
</style></head><body><div class="box">
<h2>Link Expired</h2>
<p>This offer link has expired or is invalid.<br>
Ask the sending agent to resend the offer email.</p>
<p style="margin-top:1rem;"><a href="/">Back to home</a></p></div></body></html>"""


@app.route("/thread/<path:filename>", methods=["GET", "POST"])
def offer_thread(filename):
    if ".." in filename or filename.startswith("/"):
        abort(400)
    expires = request.values.get("expires")
    sig = request.values.get("sig")
    if not verify_thread_signature(filename, expires, sig):
        return THREAD_EXPIRED_HTML, 403

    offer = get_offer_by_filename(filename)
    if not offer or not offer["price"]:
        abort(404)

    if request.method == "POST":
        action = request.form.get("action", "")
        action_sig = request.form.get("action_sig", "")
        action_expires = request.form.get("action_expires", "")
        if not verify_thread_action(filename, action, action_expires, action_sig):
            abort(403)
        if record_thread_response(filename, action):
            track_event(f"thread_{action}ed", offer["phone"], {"filename": filename, "address": offer["address"]})
            verb = "accepted" if action == "accept" else "declined"
            twilio_send_sms(
                offer["phone"],
                f"Listing agent {verb} your offer on {offer['address']}. View: "
                + sign_thread_url(filename, request.host_url.rstrip("/")),
            )
        return redirect(f"/thread/{filename}?expires={expires}&sig={sig}")

    track_event("thread_viewed", offer["phone"], {"filename": filename})

    address = offer["address"]
    price = offer["price"]
    down_pct = offer["down_pct"]
    close_days = offer["close_days"]
    down_amt = int(price * down_pct) if price else 0
    loan_amt = price - down_amt if price else 0
    thread_status = offer.get("thread_status") or "pending"

    # Attribution card: Professional-plan feature. Only renders for a real,
    # filled-in agent profile on a Professional/Brokerage plan -- never for
    # the anonymous demo (source_id "demo-web") or its placeholder values
    # ("Your Name Here" etc), which would look like a fake identity on a
    # page a real listing agent might open, and never for Starter, which
    # doesn't include agent branding.
    agent_card_html = ""
    sending_phone = offer.get("phone") or ""
    if sending_phone and sending_phone != "demo-web" and has_professional_access(sending_phone):
        sending_agent = get_agent_profile(sending_phone)
        agent_name = (sending_agent.get("name") or "").strip()
        if agent_name:
            agent_brokerage = (sending_agent.get("brokerage") or "").strip()
            agent_license = (sending_agent.get("license") or "").strip()
            agent_meta_parts = [p for p in [agent_brokerage, f"License #{agent_license}" if agent_license else ""] if p]
            agent_meta = " &middot; ".join(agent_meta_parts)
            agent_initials = "".join(p[0].upper() for p in agent_name.split()[:2]) or "TX"
            agent_card_html = f"""
<div class="agent-card">
  <div class="agent-avatar">{agent_initials}</div>
  <div>
    <div class="agent-name">{agent_name}</div>
    {f'<div class="agent-meta">{agent_meta}</div>' if agent_meta else ''}
  </div>
</div>"""

    pdf_expires, pdf_sig = sign_pdf_view_params(filename)
    pdf_url = f"/offers/{filename}?expires={pdf_expires}&sig={pdf_sig}"

    pdf_path_on_disk = os.path.join(OUTPUT_DIR, filename)
    validation = validate_offer_pdf(pdf_path_on_disk, {
        "price": price, "down_payment_amount": down_amt, "loan_amount": loan_amt,
        "close_days": close_days, "created_at": offer.get("created_at"),
        "financing_type_specified": bool(offer.get("financing_type")),
    }) if os.path.exists(pdf_path_on_disk) else {"ok": False, "blocking": [], "warnings": []}

    from datetime import timedelta
    try:
        created_dt = datetime.fromisoformat(offer["created_at"])
    except (KeyError, TypeError, ValueError):
        created_dt = datetime.now()
    close_date = (created_dt + timedelta(days=close_days)).strftime("%B %d, %Y") if close_days else ""

    action_expires = int(time.time()) + THREAD_LINK_TTL
    accept_sig = sign_thread_action(filename, "accept", action_expires)
    decline_sig = sign_thread_action(filename, "decline", action_expires)

    if thread_status == "pending":
        response_block = f"""
<div class="actions">
<form method="post" style="margin:0;">
<input type="hidden" name="action" value="accept">
<input type="hidden" name="action_sig" value="{accept_sig}">
<input type="hidden" name="action_expires" value="{action_expires}">
<input type="hidden" name="expires" value="{expires}">
<input type="hidden" name="sig" value="{sig}">
<button type="submit" class="btn btn-primary" style="width:100%;">Accept</button>
</form>
<form method="post" style="margin:0;">
<input type="hidden" name="action" value="decline">
<input type="hidden" name="action_sig" value="{decline_sig}">
<input type="hidden" name="action_expires" value="{action_expires}">
<input type="hidden" name="expires" value="{expires}">
<input type="hidden" name="sig" value="{sig}">
<button type="submit" class="btn btn-outline" style="width:100%;">Decline</button>
</form>
</div>"""
    else:
        responded_label = "Accepted" if thread_status == "accept" or thread_status == "accepted" else "Declined"
        response_block = f"""
<div class="status-panel">You marked this <strong>{responded_label}</strong> on {offer.get('thread_responded_at', '')[:10]}.</div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Offer Thread — {address}</title>
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
:root{{--bg:#F5F5F7;--bg-card:#fff;--border:rgba(15,31,47,0.08);
--text:#0f1f2f;--text-muted:#5a6b7a;--text-dim:#8a9aa9;--accent:#171717;--accent-light:#525252;
--accent-dark:#000000;--accent-tint:#F0F0EE;--radius:1.25rem;--radius-sm:0.85rem;}}
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;
-webkit-font-smoothing:antialiased;}}
.top-bar{{background:var(--accent-tint);border-bottom:1px solid rgba(23,23,23,0.2);
padding:0.6rem 1.5rem;text-align:center;font-size:0.8rem;color:var(--accent-dark);font-weight:600;}}
.container{{max-width:600px;margin:0 auto;padding:1.5rem 1rem;}}
.address-card{{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
padding:1.5rem;text-align:center;margin-bottom:1rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);}}
.address-card h1{{font-size:1.25rem;font-weight:700;margin-bottom:0.25rem;color:var(--text);}}
.address-card .meta{{color:var(--text-dim);font-size:0.8rem;}}
.agent-card{{display:flex;align-items:center;gap:0.85rem;background:var(--bg-card);border:1px solid var(--border);
border-radius:var(--radius-sm);padding:0.9rem 1.1rem;margin-bottom:1rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);}}
.agent-avatar{{width:42px;height:42px;border-radius:50%;background:var(--accent-tint);color:var(--accent-dark);
display:flex;align-items:center;justify-content:center;font-weight:700;font-size:0.9rem;flex-shrink:0;}}
.agent-name{{font-size:0.9rem;font-weight:700;color:var(--text);}}
.agent-meta{{font-size:0.78rem;color:var(--text-dim);margin-top:0.1rem;}}
.stats{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:0.5rem;margin-bottom:1rem;}}
.stat{{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-sm);
padding:0.85rem 0.5rem;text-align:center;box-shadow:0 1px 3px rgba(15,31,47,0.05);}}
.stat-label{{font-size:0.65rem;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;
color:var(--text-dim);margin-bottom:0.2rem;}}
.stat-value{{font-size:1rem;font-weight:700;color:var(--text);}}
.stat-value.accent{{color:var(--accent-dark);}}
.actions{{display:flex;flex-direction:column;gap:0.6rem;margin-bottom:1.25rem;}}
.btn{{display:flex;align-items:center;justify-content:center;gap:0.5rem;padding:0.9rem 1rem;
border-radius:var(--radius-sm);font-family:inherit;font-size:0.9rem;font-weight:600;
text-decoration:none;border:none;cursor:pointer;transition:all 0.2s;}}
.btn-primary{{background:linear-gradient(135deg,var(--accent),#000000);color:#fff;}}
.btn-primary:hover{{transform:translateY(-1px);box-shadow:0 6px 20px rgba(23,23,23,0.25);}}
.btn-outline{{background:transparent;color:var(--text-muted);border:1px solid var(--border);}}
.btn-outline:hover{{border-color:var(--accent);color:var(--accent-dark);}}
.pdf-frame{{width:100%;height:70vh;border:1px solid var(--border);border-radius:var(--radius-sm);
background:#f1f5f9;}}
.disclaimer{{font-size:0.75rem;color:var(--text-dim);text-align:center;padding:1rem;
border-top:1px solid var(--border);margin-top:1rem;}}
.notbinding{{font-size:0.78rem;color:var(--text-muted);background:var(--bg-card);border:1px solid var(--border);
border-radius:var(--radius-sm);padding:0.75rem 1rem;margin-bottom:1.25rem;line-height:1.5;}}
.status-panel{{background:var(--accent-tint);border:1px solid rgba(23,23,23,0.25);color:var(--accent-dark);
border-radius:var(--radius-sm);padding:0.9rem 1rem;text-align:center;font-size:0.9rem;margin-bottom:1.25rem;}}
.recipient-cta{{background:var(--accent-tint);border:1px solid rgba(23,23,23,0.2);border-radius:var(--radius-sm);
padding:1.1rem 1.25rem;margin-top:1.25rem;text-align:center;}}
.recipient-cta .cta-title{{font-size:0.92rem;font-weight:700;color:var(--text);margin-bottom:0.35rem;}}
.recipient-cta .cta-body{{font-size:0.82rem;color:var(--text-muted);line-height:1.5;margin-bottom:0.85rem;}}
.recipient-cta .cta-btn{{display:inline-block;background:var(--accent);color:#fff;padding:0.65rem 1.4rem;
border-radius:999px;font-size:0.85rem;font-weight:600;text-decoration:none;}}
.recipient-cta .cta-btn:hover{{background:var(--accent-light);}}
.qa-blocking, .qa-warnings{{border-radius:var(--radius-sm);padding:0.85rem 1rem;margin-bottom:0.85rem;font-size:0.85rem;}}
.qa-blocking{{background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.25);color:#dc2626;}}
.qa-warnings{{background:rgba(245,158,11,0.10);border:1px solid rgba(245,158,11,0.3);color:#b45309;}}
.qa-blocking strong, .qa-warnings strong{{display:block;margin-bottom:0.4rem;color:var(--text);}}
.qa-blocking ul, .qa-warnings ul{{margin:0;padding-left:1.1rem;}}
.qa-blocking li, .qa-warnings li{{margin-bottom:0.2rem;}}
@media(max-width:400px){{
.stats{{grid-template-columns:1fr 1fr;}}
.stat:last-child{{grid-column:span 2;}}
}}
</style>
</head>
<body>
<div class="top-bar">TREC 20-19 (mandatory as of {TREC_FORM_CURRENT_AS_OF}) — Offer sent to you via TxtAnOffer</div>
<div class="container">
{agent_card_html}
<div class="address-card">
<h1>{address}</h1>
<div class="meta">TREC One to Four Family Residential Contract</div>
</div>

<div class="stats">
<div class="stat"><div class="stat-label">Price</div><div class="stat-value accent">${price:,}</div></div>
<div class="stat"><div class="stat-label">Down</div><div class="stat-value">{down_pct*100:.0f}% (${down_amt:,})</div></div>
<div class="stat"><div class="stat-label">Close</div><div class="stat-value">{close_date}</div></div>
</div>

{'<div class="qa-blocking"><strong>Heads up &mdash; this draft is missing required fields:</strong><ul>' + ''.join(f'<li>{b}</li>' for b in validation['blocking']) + '</ul></div>' if validation['blocking'] else ''}
{'<div class="qa-warnings"><strong>Heads up:</strong><ul>' + ''.join(f'<li>{w}</li>' for w in validation['warnings']) + '</ul></div>' if validation['warnings'] else ''}

<div class="notbinding">Clicking Accept or Decline sends a quick notification to the buyer's agent. This is not a binding acceptance of the contract and is not an electronic signature &mdash; legal execution of the TREC 20-19 still requires normal signing.</div>

{response_block}

<iframe src="{pdf_url}" class="pdf-frame" title="Offer PDF"></iframe>

<div class="recipient-cta">
  <div class="cta-title">Curious how this got here?</div>
  <div class="cta-body">This offer was drafted and field-checked in about 10 seconds &mdash; text your terms, get back a reviewed TREC 20-19. Try it yourself, free, no card required.</div>
  <a href="/?src=thread_recipient" class="cta-btn">Try TxtAnOffer &rarr;</a>
</div>

<div class="disclaimer">
This is a draft generated by TxtAnOffer. Not affiliated with TREC. &middot; <a href="/" style="color:var(--accent-dark);">txtanoffer.com</a>
</div>
</div>
</body>
</html>"""


@app.route("/offers/<path:filename>")
def serve_offer(filename):
    if ".." in filename or filename.startswith("/"):
        abort(400)
    expires = request.args.get("expires")
    sig = request.args.get("sig")
    if not verify_pdf_signature(filename, expires, sig):
        abort(403)
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=False)


@app.route("/offers/<path:filename>/preview.png")
def serve_offer_preview(filename):
    """Renders one PDF page to a PNG so review pages can show a real preview
    inline without embedding the browser's own PDF viewer chrome (dark
    toolbar, thumbnail rail) which clashes with the site's UI."""
    if ".." in filename or filename.startswith("/"):
        abort(400)
    expires = request.args.get("expires")
    sig = request.args.get("sig")
    if not verify_pdf_signature(filename, expires, sig):
        abort(403)
    page_num = request.args.get("page", "0")
    page_num = int(page_num) if page_num.isdigit() else 0

    pdf_path = os.path.join(OUTPUT_DIR, filename)
    if not os.path.exists(pdf_path):
        abort(404)

    import fitz
    doc = fitz.open(pdf_path)
    page_num = max(0, min(page_num, len(doc) - 1))
    pix = doc[page_num].get_pixmap(matrix=fitz.Matrix(2, 2))
    png_bytes = pix.tobytes("png")
    doc.close()

    resp = make_response(png_bytes)
    resp.headers["Content-Type"] = "image/png"
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


# --- Dashboard auth (magic link) ------------------------------------------

DASHBOARD_LINK_TTL = int(os.environ.get("DASHBOARD_LINK_TTL", 604800))  # 7 days


def sign_dashboard_url(phone, base_url=""):
    expires = int(time.time()) + DASHBOARD_LINK_TTL
    sig = hmac.new(PDF_LINK_SECRET.encode(), f"dash:{phone}:{expires}".encode(), hashlib.sha256).hexdigest()[:20]
    # Phone starts with "+" -- must be percent-encoded (%2B) or query-string parsing
    # (which treats literal "+" as a space) corrupts it and every signature check fails.
    return f"{base_url}/dashboard?phone={_urlquote(phone, safe='')}&expires={expires}&sig={sig}"


def verify_dashboard_signature(phone, expires_str, sig):
    try:
        expires = int(expires_str)
    except (ValueError, TypeError):
        return False
    if time.time() > expires:
        return False
    expected = hmac.new(PDF_LINK_SECRET.encode(), f"dash:{phone}:{expires}".encode(), hashlib.sha256).hexdigest()[:20]
    return hmac.compare_digest(sig or "", expected)


def sign_wins_url(phone, base_url=""):
    # Deliberately non-expiring, unlike sign_dashboard_url -- this link is meant
    # to be posted publicly (social, group chats) and must keep working whenever
    # someone clicks it later, not just within a short private-session window.
    sig = hmac.new(PDF_LINK_SECRET.encode(), f"wins:{phone}".encode(), hashlib.sha256).hexdigest()[:20]
    return f"{base_url}/wins?phone={_urlquote(phone, safe='')}&sig={sig}"


def verify_wins_signature(phone, sig):
    expected = hmac.new(PDF_LINK_SECRET.encode(), f"wins:{phone}".encode(), hashlib.sha256).hexdigest()[:20]
    return hmac.compare_digest(sig or "", expected)


@app.route("/dashboard")
def dashboard():
    phone = request.args.get("phone", "")
    expires = request.args.get("expires", "")
    sig = request.args.get("sig", "")

    if not verify_dashboard_signature(phone, expires, sig):
        return """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Dashboard - TxtAnOffer</title>
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet"></noscript>
<style>
body{font-family:'Inter',sans-serif;background:#F5F5F7;color:#0f1f2f;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px;}
.box{background:#fff;border-radius:1.25rem;padding:2.5rem;max-width:400px;text-align:center;
border:1px solid rgba(15,31,47,0.08);box-shadow:0 1px 2px rgba(15,31,47,0.04);}
h2{margin:0 0 0.75rem;font-size:1.35rem;font-weight:700;}
p{color:#5a6b7a;font-size:0.9rem;line-height:1.6;}
a{color:#000000;text-decoration:none;}
a:hover{text-decoration:underline;}
</style></head><body><div class="box">
<h2>Access Expired</h2>
<p>Your dashboard link has expired or is invalid.<br>
Text <strong>DASHBOARD</strong> to (833) 897-0333 to get a fresh link.</p>
<p style="margin-top:1rem;"><a href="/">Back to home</a></p></div></body></html>""", 403

    user = get_user(phone)
    if not user:
        return redirect("/signup")

    from agent_profiles import get_agent_profile
    agent = get_agent_profile(phone)
    offers = get_offers_for_phone(phone)
    amendments_by_offer = get_amendments_for_phone(phone)
    from datetime import timedelta

    # Build offer cards, with each offer's amendments nested inside the same card
    offer_cards = ""
    accepted_volume = 0
    accepted_count = 0
    for o in offers:
        pdf_link = sign_pdf_url(o["filename"], request.host_url.rstrip("/"))
        created = o["created_at"][:10]

        # Listing-agent response (via the Offer Thread link) takes priority
        # over the closing-date-derived fallback below, once one exists.
        try:
            created_dt = datetime.fromisoformat(o["created_at"])
        except ValueError:
            created_dt = datetime.utcnow()
        close_dt = created_dt + timedelta(days=o.get("close_days") or 0)
        if o.get("thread_status") in ("accept", "decline"):
            status = "accepted" if o["thread_status"] == "accept" else "declined"
        else:
            status = "expired" if close_dt < datetime.utcnow() else "draft"

        if status == "accepted":
            accepted_volume += o["price"]
            accepted_count += 1

        amend_html = ""
        for a in amendments_by_offer.get(o["id"], []):
            a_pdf_link = sign_pdf_url(a["filename"], request.host_url.rstrip("/"))
            a_created = a["created_at"][:10]
            a_desc = f"New price ${a['value']:,}" if a["field"] == "price" else f"Closing +{a['value']}d"
            amend_html += f"""
            <div class="amend-row">
              <span class="amend-desc">&#8618; {a_desc}</span>
              <span class="amend-date">{a_created}</span>
              <a href="{a_pdf_link}" target="_blank" class="amend-pdf">PDF</a>
            </div>"""

        offer_cards += f"""
        <div class="offer-card">
          <div class="offer-card-bar status-{status}"></div>
          <div class="offer-card-body">
            <div class="offer-top">
              <div class="offer-addr-wrap">
                <div class="offer-addr">{o['address']}</div>
                <span class="status-badge status-{status}">{status}</span>
              </div>
              <div class="offer-date">{created}</div>
            </div>
            <div class="pills">
              <div class="pill"><span class="pill-val">${o['price']:,}</span><span class="pill-label">Price</span></div>
              <div class="pill"><span class="pill-val">{o['down_pct']*100:.0f}%</span><span class="pill-label">Down</span></div>
              <div class="pill"><span class="pill-val">{o['close_days']}d</span><span class="pill-label">Close</span></div>
            </div>
            {f'<div class="amend-list">{amend_html}</div>' if amend_html else ''}
            <a href="{pdf_link}" target="_blank" class="btn-primary">View PDF</a>
          </div>
        </div>"""

    if not offer_cards:
        offer_cards = '<div class="empty-state">No offers yet.<br>Text your first offer to get started.</div>'

    def _fmt_time_saved(minutes: int) -> str:
        if minutes < 60:
            return f"{minutes}m"
        h, m = divmod(minutes, 60)
        return f"{h}h {m}m" if m else f"{h}h"

    time_saved = _fmt_time_saved(user["offer_count"] * 45)
    avg_close = f"{round(sum(o['close_days'] for o in offers) / len(offers))}d" if offers else "—"
    wins_url = sign_wins_url(phone, request.host_url.rstrip("/"))

    if accepted_count > 0:
        milestone_html = f"""
      <div class="milestone-logo"><img src="/static/logo.png" alt=""></div>
      <div class="milestone-val">Congrats on ${accepted_volume:,}!</div>
      <div class="milestone-sub">{accepted_count} offer{'s' if accepted_count != 1 else ''} accepted through TxtAnOffer</div>
      <a href="{wins_url}" target="_blank" class="milestone-share">Share your milestone &rarr;</a>"""
    else:
        milestone_html = """
      <div class="milestone-logo"><img src="/static/logo.png" alt=""></div>
      <div class="milestone-sub">Your first accepted offer will show up here.</div>"""

    initials = "".join(part[0] for part in agent.get("name", "").split()[:2]).upper() if agent.get("name") else "?"

    if is_admin_phone(phone):
        sub_status = "Admin (Unlimited)"
        sub_badge_color = "rgba(124,58,237,0.10)"
        sub_badge_text = "#7c3aed"
    elif user["is_subscribed"]:
        sub_status = "Active"
        sub_badge_color = "var(--accent-tint)"
        sub_badge_text = "#000000"
    else:
        sub_status = f"Free ({user['offer_count']}/{FREE_OFFER_LIMIT} used)"
        sub_badge_color = "rgba(245,158,11,0.12)"
        sub_badge_text = "#b45309"

    profile_url = f"/profile?phone={_urlquote(phone, safe='')}&expires={expires}&sig={sig}"

    def _pf(label, value, fallback="Not set"):
        shown = value if value else fallback
        cls = "profile-field-val" if value else "profile-field-val unset"
        return f'<div><div class="profile-field-label">{label}</div><div class="{cls}">{shown}</div></div>'

    has_profile = any(agent.get(k) for k in ("name", "license", "brokerage", "email", "title_company"))
    if has_profile:
        id_card = f"""
        <div class="id-card">
          <div class="avatar">{initials}</div>
          <div class="id-card-info">
            <div class="id-name">{agent.get("name") or "Not set"}</div>
            <div class="id-meta">{f'TREC #{agent["license"]}' if agent.get("license") else ""}{' &middot; ' if agent.get("license") and agent.get("brokerage") else ""}{agent.get("brokerage") or ""}</div>
          </div>
        </div>"""
        profile_body = f"""
        {id_card}
        <div class="profile-grid">
          {_pf("Email", agent.get("email"))}
          {_pf("Business Address", agent.get("business_address"))}
          {_pf("Title Company", agent.get("title_company"))}
          {_pf("Title Company Address", agent.get("title_company_address"))}
          {_pf("Default Earnest %", f"{agent['default_earnest_pct']*100:.1f}%" if agent.get("default_earnest_pct") else None)}
          {_pf("Default Option Fee", f"${agent['default_option_fee']:,}" if agent.get("default_option_fee") else None)}
        </div>"""
    else:
        profile_body = f'<p class="profile-empty">Not set up yet. Your name, license, and brokerage auto-fill into every contract once saved. <a href="{profile_url}">Set up your profile &rarr;</a></p>'

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard — TxtAnOffer</title>
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
  :root{{
    --bg: #F5F5F7;
    --bg-card: #fff;
    --border: rgba(15,31,47,0.08);
    --border-hover: rgba(23,23,23,0.35);
    --text: #0f1f2f;
    --text-muted: #5a6b7a;
    --text-dim: #8a9aa9;
    --accent: #171717;
    --accent-light: #525252;
    --accent-dark: #000000;
    --accent-tint: #F0F0EE;
    --radius: 1.25rem;
    --radius-sm: 0.85rem;
    --transition: all 0.2s ease;
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
    background:var(--bg);
    color:var(--text);
    line-height:1.5;
    -webkit-font-smoothing:antialiased;
    min-height:100vh;
  }}
  a {{ color:inherit; text-decoration:none; }}

  .nav {{
    display:flex;align-items:center;justify-content:space-between;
    padding:1rem 2rem;position:sticky;top:0;
    background:rgba(255,255,255,0.85);backdrop-filter:blur(20px);
    -webkit-backdrop-filter:blur(20px);
    border-bottom:1px solid var(--border);z-index:100;
  }}
  .nav-left {{display:flex;align-items:center;gap:0.6rem;font-weight:700;font-size:1.1rem;letter-spacing:-0.02em;color:var(--text);}}
  .nav-logo {{width:34px;height:34px;border-radius:22%;overflow:hidden;}}
  .nav-logo img {{width:100%;height:100%;object-fit:contain;}}
  .nav-links {{display:flex;gap:2rem;font-size:0.875rem;font-weight:500;color:var(--text-muted);}}
  .nav-links a {{transition:var(--transition);}}
  .nav-links a:hover {{color:var(--text);}}
  .nav-toggle {{ display: none; flex-direction: column; justify-content: center; gap: 5px; width: 34px; height: 34px; background: none; border: none; cursor: pointer; padding: 0; }}
  .nav-toggle span {{ display: block; width: 100%; height: 2px; background: var(--text); border-radius: 2px; }}

  .container {{max-width:1000px;margin:0 auto;padding:2.5rem 2rem 4rem;}}
  .greeting {{font-size:1.75rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.5rem;color:var(--text);}}
  .sub-badge {{
    display:inline-block;background:{sub_badge_color};color:{sub_badge_text};
    padding:0.3rem 0.85rem;border-radius:9999px;font-size:0.75rem;font-weight:700;
    letter-spacing:0.02em;
  }}

  .milestone-card {{
    text-align:center;margin-top:1.5rem;border-radius:var(--radius);
    background:var(--bg-card);border:1px solid var(--border);
    padding:2.5rem 2rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);
  }}
  .milestone-logo {{width:44px;height:44px;margin:0 auto 1.25rem;border-radius:22%;overflow:hidden;}}
  .milestone-logo img {{width:100%;height:100%;object-fit:contain;}}
  .milestone-val {{font-size:2.25rem;font-weight:800;letter-spacing:-0.02em;line-height:1.1;color:var(--text);}}
  .milestone-sub {{font-size:0.85rem;color:var(--text-muted);margin-top:0.5rem;}}
  .milestone-share {{
    display:inline-block;margin-top:1.5rem;font-size:0.82rem;font-weight:600;
    color:var(--text);border-bottom:1px solid var(--border-hover);padding-bottom:0.1rem;
    transition:var(--transition);
  }}
  .milestone-share:hover {{color:var(--accent-dark);}}

  .stats {{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1rem;margin:2rem 0;}}
  .stat {{
    background:var(--bg-card);
    border:1px solid var(--border);border-radius:var(--radius);
    padding:1.5rem;transition:var(--transition);
    box-shadow:0 1px 3px rgba(15,31,47,0.05);
  }}
  .stat:hover {{border-color:var(--border-hover);transform:translateY(-1px);}}
  .stat-val {{
    font-size:1.75rem;font-weight:800;
    background:linear-gradient(135deg, var(--accent), var(--accent-light));
    -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;
  }}
  .stat-label {{font-size:0.7rem;font-weight:600;color:var(--text-dim);margin-top:0.25rem;
    text-transform:uppercase;letter-spacing:0.06em;}}

  .profile-card {{
    background:var(--bg-card);
    border:1px solid var(--border);border-radius:var(--radius);
    padding:1.5rem 1.75rem;margin-top:0.5rem;
    box-shadow:0 1px 3px rgba(15,31,47,0.05);
  }}
  .profile-card-head {{display:flex;align-items:center;justify-content:space-between;margin-bottom:1rem;}}
  .profile-card-head h2 {{margin:0;}}
  .profile-edit-link {{
    font-size:0.8rem;font-weight:600;color:var(--accent-dark);
    border:1px solid var(--border);border-radius:var(--radius-sm);padding:0.4rem 0.85rem;
    transition:var(--transition);
  }}
  .profile-edit-link:hover {{border-color:var(--accent);}}

  .id-card {{display:flex;align-items:center;gap:1rem;margin-bottom:1.5rem;padding-bottom:1.5rem;
    border-bottom:1px solid var(--border);}}
  .avatar {{
    width:52px;height:52px;border-radius:50%;flex-shrink:0;
    background:linear-gradient(135deg, var(--accent), var(--accent-light));
    display:flex;align-items:center;justify-content:center;
    font-weight:800;font-size:1.1rem;color:#06281d;
  }}
  .id-name {{font-weight:700;font-size:1.05rem;color:var(--text);}}
  .id-meta {{font-size:0.8rem;color:var(--text-muted);margin-top:0.2rem;}}

  .profile-grid {{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:1.1rem;}}
  .profile-field-label {{font-size:0.7rem;font-weight:600;color:var(--text-dim);
    text-transform:uppercase;letter-spacing:0.06em;margin-bottom:0.25rem;}}
  .profile-field-val {{font-size:0.9rem;color:var(--text);}}
  .profile-field-val.unset {{color:var(--text-dim);font-style:italic;}}
  .profile-empty {{color:var(--text-muted);font-size:0.9rem;line-height:1.6;}}
  .profile-empty a {{color:var(--accent-dark);font-weight:600;}}

  h2 {{font-size:1.1rem;font-weight:700;margin:2.5rem 0 1rem;color:var(--text);}}

  .offer-feed {{display:flex;flex-direction:column;gap:0.9rem;}}
  .offer-card {{
    display:flex;border-radius:var(--radius);overflow:hidden;
    background:var(--bg-card);
    border:1px solid var(--border);
    box-shadow:0 1px 3px rgba(15,31,47,0.05);
    transition:var(--transition);
  }}
  .offer-card:hover {{border-color:var(--border-hover);box-shadow:0 4px 16px rgba(15,31,47,0.08);}}
  .offer-card:active {{transform:scale(0.99);}}
  .offer-card-bar {{width:4px;flex-shrink:0;background:linear-gradient(180deg, var(--accent), var(--accent-light));}}
  .offer-card-bar.status-draft {{background:linear-gradient(180deg, #171717, #525252);}}
  .offer-card-bar.status-sent {{background:linear-gradient(180deg, #f59e0b, #fbbf24);}}
  .offer-card-bar.status-expired {{background:linear-gradient(180deg, #9ca3af, #cbd5e1);}}
  .offer-card-bar.status-accepted {{background:linear-gradient(180deg, #3b82f6, #60a5fa);}}
  .offer-card-bar.status-declined {{background:linear-gradient(180deg, #f43f5e, #fb7185);}}
  .offer-card-body {{padding:1.25rem 1.5rem;flex:1;min-width:0;}}
  .offer-top {{display:flex;align-items:baseline;justify-content:space-between;gap:0.75rem;margin-bottom:0.9rem;}}
  .offer-addr-wrap {{display:flex;align-items:center;gap:0.6rem;min-width:0;}}
  .offer-addr {{font-weight:700;font-size:0.98rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text);}}
  .offer-date {{font-size:0.75rem;color:var(--text-dim);flex-shrink:0;}}
  .status-badge {{
    font-size:0.65rem;font-weight:700;text-transform:uppercase;letter-spacing:0.04em;
    padding:0.2rem 0.55rem;border-radius:9999px;flex-shrink:0;
  }}
  .status-badge.status-draft {{background:rgba(23,23,23,0.12);color:#000000;}}
  .status-badge.status-sent {{background:rgba(245,158,11,0.14);color:#b45309;}}
  .status-badge.status-expired {{background:rgba(148,163,184,0.18);color:#64748b;}}
  .status-badge.status-accepted {{background:rgba(59,130,246,0.12);color:#2563eb;}}
  .status-badge.status-declined {{background:rgba(244,63,94,0.12);color:#e11d48;}}

  .pills {{display:flex;gap:0.6rem;margin-bottom:1rem;flex-wrap:wrap;}}
  .pill {{
    background:rgba(15,31,47,0.03);border:1px solid var(--border);border-radius:var(--radius-sm);
    padding:0.5rem 0.85rem;display:flex;flex-direction:column;gap:0.1rem;min-width:72px;
  }}
  .pill-val {{font-size:0.9rem;font-weight:700;color:var(--text);}}
  .pill-label {{font-size:0.65rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.05em;}}

  .amend-list {{margin:0 0 1rem;padding:0.75rem 0.9rem;background:rgba(15,31,47,0.02);
    border:1px solid var(--border);border-radius:var(--radius-sm);}}
  .amend-row {{display:flex;align-items:center;justify-content:space-between;gap:0.75rem;
    font-size:0.78rem;color:var(--text-dim);padding:0.25rem 0;}}
  .amend-desc {{color:var(--text-muted);}}
  .amend-pdf {{color:var(--accent-dark);font-weight:600;flex-shrink:0;}}
  .amend-pdf:hover {{text-decoration:underline;}}

  .btn-primary {{
    display:inline-block;background:var(--accent);color:#fff;font-weight:700;
    padding:0.6rem 1.1rem;border-radius:var(--radius-sm);font-size:0.85rem;
    box-shadow:0 2px 10px rgba(23,23,23,0.25);transition:var(--transition);
  }}
  .btn-primary:hover {{background:var(--accent-dark);}}
  .btn-primary:active {{transform:scale(0.97);}}

  .empty-state {{
    text-align:center;color:var(--text-dim);padding:3rem 1.5rem;
    background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
    line-height:1.7;
  }}

  .bottom-nav {{
    position:sticky;bottom:0;display:flex;justify-content:space-around;
    background:rgba(255,255,255,0.9);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
    border-top:1px solid var(--border);padding:0.7rem 0 calc(0.7rem + env(safe-area-inset-bottom));
    margin-top:2.5rem;
  }}
  .nav-item {{
    display:flex;flex-direction:column;align-items:center;gap:0.2rem;
    font-size:0.65rem;font-weight:600;color:var(--text-dim);transition:var(--transition);
  }}
  .nav-item span.icon {{font-size:1.2rem;}}
  .nav-item:hover, .nav-item.active {{color:var(--accent-dark);}}

  @media(max-width:600px){{
    .container {{padding:1.5rem 1rem 1rem;}}
    .stats {{grid-template-columns:1fr 1fr 1fr;}}
    .greeting {{font-size:1.35rem;}}
    .nav-toggle {{ display: flex; }}
    .nav-links {{
      display: none; position: absolute; top: 100%; left: 0; right: 0;
      flex-direction: column; gap: 0; padding: 0.5rem 1.25rem 1.25rem;
      background: #fff; border-bottom: 1px solid rgba(15,31,47,0.08);
    }}
    .nav-links.open {{ display: flex; }}
    .nav-links a {{ padding: 0.75rem 0; border-bottom: 1px solid rgba(15,31,47,0.08); }}
    .nav-links a:last-child {{ border-bottom: none; }}
    .offer-top {{flex-direction:column;align-items:flex-start;gap:0.15rem;}}
  }}
</style>
</head>
<body>
<nav class="nav">
  <a href="/" class="nav-left">
    <div class="nav-logo"><img src="/static/logo.svg" alt="TxtAnOffer"></div>
    <span>TxtAnOffer</span>
  </a>
  <div class="nav-links" id="navLinks">
    <a href="{profile_url}">Edit Profile</a>
    <a href="/pricing">Pricing</a>
  </div>
  <button class="nav-toggle" id="navToggle" aria-label="Menu" aria-expanded="false"><span></span><span></span><span></span></button>
</nav>
<script>
(function(){{
  var t=document.getElementById('navToggle'), l=document.getElementById('navLinks');
  if(!t||!l) return;
  t.addEventListener('click', function(){{
    var open = l.classList.toggle('open');
    t.setAttribute('aria-expanded', open ? 'true' : 'false');
  }});
  l.querySelectorAll('a').forEach(function(a){{
    a.addEventListener('click', function(){{ l.classList.remove('open'); t.setAttribute('aria-expanded','false'); }});
  }});
}})();
</script>

<div class="container">
  <div class="greeting">Welcome back{', ' + agent.get('name').split()[0] if agent.get('name') else ''}</div>
  <span class="sub-badge">{sub_status}</span>

  <div class="stats">
    <div class="stat"><div class="stat-val">{user['offer_count']}</div><div class="stat-label">Total offers</div></div>
    <div class="stat"><div class="stat-val">{time_saved}</div><div class="stat-label">Time saved</div></div>
    <div class="stat"><div class="stat-val">{avg_close}</div><div class="stat-label">Avg close</div></div>
  </div>

  <div class="milestone-card">
    {milestone_html}
  </div>

  <div class="profile-card">
    <div class="profile-card-head">
      <h2>Agent Profile</h2>
      <a href="{profile_url}" class="profile-edit-link">Edit</a>
    </div>
    {profile_body}
  </div>

  <h2>Offer History</h2>
  <div class="offer-feed">
    {offer_cards}
  </div>
</div>

<nav class="bottom-nav">
  <a href="/dashboard?phone={_urlquote(phone, safe='')}&expires={expires}&sig={sig}" class="nav-item active"><span class="icon">&#8962;</span>Dashboard</a>
  <a href="{profile_url}" class="nav-item"><span class="icon">&#128100;</span>Profile</a>
  <a href="/pricing" class="nav-item"><span class="icon">&#128179;</span>{'Billing' if user['is_subscribed'] else 'Upgrade'}</a>
</nav>
</body>
</html>"""


@app.route("/wins")
def wins_page():
    phone = request.args.get("phone", "")
    sig = request.args.get("sig", "")

    if not verify_wins_signature(phone, sig):
        abort(404)

    user = get_user(phone)
    if not user:
        abort(404)

    from agent_profiles import get_agent_profile
    agent = get_agent_profile(phone)
    offers = get_offers_for_phone(phone)

    accepted_volume = 0
    accepted_count = 0
    for o in offers:
        try:
            created_dt = datetime.fromisoformat(o["created_at"])
        except ValueError:
            created_dt = datetime.utcnow()
        close_dt = created_dt + timedelta(days=o.get("close_days") or 0)
        if o.get("thread_status") == "accept":
            status = "accepted"
        elif o.get("thread_status") == "decline":
            status = "declined"
        else:
            status = "expired" if close_dt < datetime.utcnow() else "draft"
        if status == "accepted":
            accepted_volume += o["price"]
            accepted_count += 1

    name = (agent.get("name") or "").strip()
    brokerage = (agent.get("brokerage") or "").strip()
    display_name = name or "A Texas Agent"
    meta_line = brokerage if brokerage else "TxtAnOffer Agent"

    page_url = request.url
    if accepted_count > 0:
        headline = f"Congrats on ${accepted_volume:,}!"
        sub = f"{accepted_count} offer{'s' if accepted_count != 1 else ''} accepted through TxtAnOffer"
        share_text = f"Just hit ${accepted_volume:,} in accepted offers through TxtAnOffer."
        share_html = f"""
    <div class="share-row">
      <a href="https://twitter.com/intent/tweet?text={_urlquote(share_text, safe='')}&url={_urlquote(page_url, safe='')}" target="_blank" rel="noopener" class="share-btn">Share on X</a>
      <a href="https://www.linkedin.com/sharing/share-offsite/?url={_urlquote(page_url, safe='')}" target="_blank" rel="noopener" class="share-btn">Share on LinkedIn</a>
    </div>"""
    else:
        headline = "Just getting started"
        sub = "First accepted offer coming soon"
        share_text = sub
        share_html = ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{display_name}'s TxtAnOffer Milestone</title>
<meta name="description" content="{sub}">
<meta property="og:title" content="{display_name}'s TxtAnOffer Milestone">
<meta property="og:description" content="{share_text}">
<meta property="og:url" content="{page_url}">
<meta property="og:type" content="website">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="{display_name}'s TxtAnOffer Milestone">
<meta name="twitter:description" content="{share_text}">
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
    background:#F5F5F7; color:#0f1f2f; min-height:100vh;
    display:flex; align-items:center; justify-content:center; padding:1.5rem;
    -webkit-font-smoothing:antialiased;
  }}
  a {{ color:inherit; text-decoration:none; }}
  .card {{
    width:100%; max-width:400px; text-align:center;
    background:#fff; border:1px solid rgba(15,31,47,0.08);
    border-radius:1.25rem; padding:2.75rem 2rem;
    box-shadow:0 1px 3px rgba(15,31,47,0.05);
  }}
  .logo {{width:48px;height:48px;margin:0 auto 1.5rem;border-radius:22%;overflow:hidden;}}
  .logo img {{width:100%;height:100%;object-fit:contain;}}
  .agent-name {{font-size:0.95rem; font-weight:700; color:#0f1f2f;}}
  .agent-meta {{font-size:0.8rem; color:#8a9aa9; margin-top:0.15rem; margin-bottom:1.75rem;}}
  .headline {{font-size:2.25rem; font-weight:800; letter-spacing:-0.02em; line-height:1.1; color:#0f1f2f; word-break:break-word;}}
  .sub {{font-size:0.85rem; color:#5a6b7a; margin-top:0.5rem;}}
  .divider {{height:1px; background:rgba(15,31,47,0.08); margin:1.75rem 0 1.5rem;}}
  .share-row {{display:flex;gap:0.6rem;margin-top:1.75rem;}}
  .share-btn {{
    flex:1; font-size:0.8rem; font-weight:600; color:#0f1f2f;
    border:1px solid rgba(15,31,47,0.14); border-radius:9999px;
    padding:0.6rem 0.5rem; transition:var(--transition, all 0.2s ease);
  }}
  .share-btn:hover {{border-color:rgba(15,31,47,0.35);background:rgba(15,31,47,0.02);}}
  .cta {{
    display:inline-block; font-size:0.85rem; font-weight:600; color:#0f1f2f;
    border-bottom:1px solid rgba(23,23,23,0.35); padding-bottom:0.1rem;
    transition:color 0.2s ease;
  }}
  .cta:hover {{color:#000;}}
  .foot {{font-size:0.72rem; color:#8a9aa9; margin-top:1.5rem;}}
  .foot a {{color:#5a6b7a; font-weight:600;}}
</style>
</head>
<body>
  <div class="card">
    <div class="logo"><img src="/static/logo.png" alt=""></div>
    <div class="agent-name">{display_name}</div>
    <div class="agent-meta">{meta_line}</div>
    <div class="headline">{headline}</div>
    <div class="sub">{sub}</div>
    {share_html}
    <div class="divider"></div>
    <a href="/" class="cta">Try TxtAnOffer free &rarr;</a>
    <div class="foot">Text your offer. Get your contract. <a href="/">txtanoffer.com</a></div>
  </div>
</body>
</html>"""
    return html


@app.route("/health")
def health():
    """Health check for uptime monitoring and Railway restart."""
    import sqlite3
    db_path = os.environ.get("DATABASE_PATH", "subscriptions.db")
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("SELECT 1")
        conn.close()
    except Exception:
        return jsonify({"status": "unhealthy", "db": "unreachable"}), 503
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
