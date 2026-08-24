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
from subscriptions import can_generate_offer, increment_offer_count, activate_subscription, deactivate_subscription, get_user, create_user, FREE_OFFER_LIMIT, is_admin_phone
from analytics import track_event, get_conversion_metrics, get_revenue_metrics, get_recent_sms, get_recent_sms_failures, get_last_blocked_state, get_waitlist_signups, get_signups_by_source
from integrations import send_offer_email, fire_webhook, save_webhook, get_webhook, delete_webhook, send_to_docusign
from offers_db import record_offer, get_offers_for_phone, get_offer_by_filename, record_amendment, get_amendments_for_phone, record_thread_response, record_email_sent
from sms_utils import parse_incoming_sms
from cleanup import run_cleanup_if_due
from reminders import run_reminders_if_due
from drafts import save_draft, get_draft, clear_draft
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
# Railway terminates TLS at its edge and forwards plain HTTP internally, so
# without this, request.host_url (used to build every SMS/PDF/checkout link)
# reports "http://" even though the site is only ever served over https.
# Trusts exactly one proxy hop (Railway's own edge) for X-Forwarded-Proto/Host.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

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
  <title>TxtAnOffer — Generate TREC Contracts by Text Message</title>
  <meta name="description" content="Texas real estate agents: text your offer details and receive a filled TREC 1-4 contract PDF in under 10 seconds, with every required field verified before it reaches you. No app required.">
  <link rel="icon" href="/static/favicon.ico" type="image/x-icon">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
  <style>
    :root {
      --bg: #0f172a;
      --bg-elevated: #1e293b;
      --bg-card: rgba(255,255,255,0.03);
      --border: rgba(255,255,255,0.06);
      --border-hover: rgba(16,185,129,0.3);
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --text-dim: #64748b;
      --accent: #10b981;
      --accent-light: #34d399;
      --accent-glow: rgba(16,185,129,0.25);
      --radius: 1.25rem;
      --radius-sm: 0.75rem;
      --shadow: 0 25px 60px rgba(0,0,0,0.5);
      --shadow-sm: 0 4px 12px rgba(0,0,0,0.15);
      --transition: all 0.2s ease;
    }

    * { margin: 0; padding: 0; box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
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
      background: rgba(15, 23, 42, 0.9);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
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
    }
    .nav-logo img {
      width: 100%; height: 100%;
      object-fit: contain;
    }
    .nav-links {
      display: flex;
      gap: 2rem;
      font-size: 0.875rem;
      font-weight: 500;
      color: var(--text-muted);
    }
    .nav-links a { transition: var(--transition); }
    .nav-links a:hover { color: var(--text); }
    .nav-cta {
      background: var(--accent);
      color: #fff;
      padding: 0.55rem 1.35rem;
      border-radius: 9999px;
      font-size: 0.875rem;
      font-weight: 600;
      border: none;
      cursor: pointer;
      transition: var(--transition);
      display: inline-block;
      text-decoration: none;
    }
    .nav-cta:hover {
      transform: scale(1.05);
      box-shadow: 0 0 24px rgba(16,185,129,0.4);
    }

    /* Hero */
    .hero {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 4rem;
      max-width: 1200px;
      margin: 0 auto;
      padding: 5rem 2rem 6rem;
      align-items: center;
    }
    .hero-left { display: flex; flex-direction: column; gap: 1.75rem; }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      background: rgba(16,185,129,0.1);
      border: 1px solid rgba(16,185,129,0.2);
      color: var(--accent-light);
      font-size: 0.7rem;
      font-weight: 700;
      padding: 0.35rem 0.85rem;
      border-radius: 9999px;
      width: fit-content;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }
    .hero h1 {
      font-size: 3.5rem;
      font-weight: 800;
      line-height: 1.05;
      letter-spacing: -0.03em;
    }
    .hero h1 .gradient {
      background: linear-gradient(135deg, var(--accent-light) 0%, var(--accent) 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
    }
    .hero-sub {
      font-size: 1.125rem;
      color: var(--text-muted);
      line-height: 1.65;
      max-width: 480px;
    }

    /* Input Card */
    .input-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1.25rem;
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
      backdrop-filter: blur(4px);
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
      background: rgba(0,0,0,0.35);
      border: 1px solid rgba(255,255,255,0.1);
      border-radius: var(--radius-sm);
      padding: 0.8rem 1rem;
      color: var(--text);
      font-size: 0.95rem;
      font-family: inherit;
      outline: none;
      transition: var(--transition);
    }
    .input-row input:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(16,185,129,0.15);
    }
    .input-row input::placeholder { color: #475569; }
    .input-btn {
      background: linear-gradient(135deg, var(--accent), #059669);
      color: #fff;
      border: none;
      border-radius: var(--radius-sm);
      padding: 0.8rem 1.5rem;
      font-weight: 600;
      font-size: 0.9rem;
      font-family: inherit;
      cursor: pointer;
      transition: var(--transition);
      white-space: nowrap;
    }
    .input-btn:hover {
      transform: translateY(-2px);
      box-shadow: 0 8px 24px rgba(16,185,129,0.35);
    }
    .input-hint { font-size: 0.75rem; color: #475569; }
    .hero-phone {
      margin-top: 0.85rem;
      padding-top: 0.85rem;
      border-top: 1px solid var(--border);
      font-size: 0.8rem;
      color: var(--text-muted);
    }
    .hero-phone a {
      color: var(--accent-light);
      font-weight: 600;
      text-decoration: none;
    }
    .hero-phone a:hover { text-decoration: underline; }

    /* Demo result */
    .demo-loading{display:none;color:var(--accent-light);font-size:0.85rem;padding:0.5rem 0;}
    .demo-error{display:none;color:#f87171;font-size:0.85rem;padding:0.5rem 0;}
    .demo-result{
      display:none;background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.2);
      border-radius:var(--radius-sm);padding:1rem;margin-top:0.5rem;
    }
    .demo-result.show{display:block;}
    .demo-result .res-row{display:flex;justify-content:space-between;padding:4px 0;font-size:0.85rem;}
    .demo-result .res-row .k{color:var(--text-dim);}
    .demo-result .res-row .v{color:#e2e8f0;font-weight:500;}
    .demo-result .res-link{
      display:inline-block;margin-top:8px;color:var(--accent-light);font-size:0.85rem;font-weight:600;text-decoration:none;
    }
    .demo-result .res-link:hover{text-decoration:underline;}

    /* Stats */
    .stats { display: flex; gap: 2.5rem; margin-top: 0.25rem; }
    .stat-num { font-size: 1.5rem; font-weight: 800; color: var(--text); line-height: 1; }
    .stat-label { font-size: 0.75rem; color: var(--text-dim); margin-top: 0.25rem; font-weight: 500; }

    /* Social Proof */
    .social-proof { display: flex; align-items: center; gap: 1rem; margin-top: 0.25rem; }
    .avatars { display: flex; }
    .avatar {
      width: 34px; height: 34px;
      border-radius: 50%;
      border: 2.5px solid var(--bg);
      margin-left: -12px;
      display: flex; align-items: center; justify-content: center;
      font-size: 10px; font-weight: 700; color: #fff;
    }
    .avatar:first-child { margin-left: 0; }
    .social-text { font-size: 0.8rem; color: var(--text-muted); }
    .social-text strong { color: #e2e8f0; }

    /* Phone Mockup */
    .phone-wrap { display: flex; justify-content: center; align-items: center; position: relative; }
    .phone-glow {
      position: absolute;
      width: 320px; height: 320px;
      background: radial-gradient(circle, var(--accent-glow) 0%, transparent 70%);
      border-radius: 50%;
      filter: blur(50px);
      z-index: 0;
      animation: pulse 4s ease-in-out infinite;
    }
    @keyframes pulse {
      0%, 100% { opacity: 0.6; transform: scale(1); }
      50% { opacity: 1; transform: scale(1.1); }
    }
    .phone {
      width: 300px;
      background: var(--bg-elevated);
      border-radius: 2.5rem;
      border: 5px solid #334155;
      padding: 1rem;
      position: relative;
      z-index: 1;
      box-shadow: var(--shadow);
    }
    .phone-notch {
      width: 90px; height: 22px;
      background: var(--bg);
      border-radius: 0 0 14px 14px;
      margin: 0 auto 0.75rem;
    }
    .phone-screen {
      background: var(--bg);
      border-radius: 1.75rem;
      padding: 1.1rem;
      min-height: 400px;
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
      overflow: hidden;
    }
    .msg-time { text-align: center; font-size: 0.65rem; color: #475569; margin-bottom: 0.25rem; }
    .msg-bubble {
      max-width: 88%;
      padding: 0.65rem 0.95rem;
      border-radius: 1.1rem;
      font-size: 0.82rem;
      line-height: 1.45;
      word-break: break-word;
      animation: slideUp 0.4s ease-out;
    }
    @keyframes slideUp {
      from { opacity: 0; transform: translateY(10px); }
      to { opacity: 1; transform: translateY(0); }
    }
    .msg-user {
      align-self: flex-end;
      background: var(--accent);
      color: #fff;
      border-bottom-right-radius: 0.3rem;
    }
    .msg-bot {
      align-self: flex-start;
      background: rgba(255,255,255,0.05);
      border: 1px solid rgba(255,255,255,0.06);
      color: #e2e8f0;
      border-bottom-left-radius: 0.3rem;
    }
    .msg-bot a { color: var(--accent-light); text-decoration: underline; text-underline-offset: 2px; }
    .msg-accepted { background: rgba(16,185,129,0.1); border-color: rgba(16,185,129,0.25); color: #d1fae5; }
    .pdf-preview {
      background: rgba(255,255,255,0.03);
      border: 1px solid rgba(255,255,255,0.06);
      border-radius: var(--radius-sm);
      padding: 0.75rem;
      display: flex;
      align-items: center;
      gap: 0.6rem;
      margin-top: 0.25rem;
      overflow: hidden;
      min-width: 0;
    }
    .pdf-icon {
      width: 36px; height: 36px;
      background: rgba(239,68,68,0.12);
      border-radius: 0.5rem;
      display: flex; align-items: center; justify-content: center;
      color: #f87171;
      font-size: 0.65rem; font-weight: 800;
      flex-shrink: 0;
    }
    .pdf-name { font-size: 0.78rem; color: #e2e8f0; font-weight: 500; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .pdf-meta { font-size: 0.68rem; color: #475569; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

    /* Steps */
    .steps {
      max-width: 1200px;
      margin: 0 auto;
      padding: 5rem 2rem;
      border-top: 1px solid var(--border);
    }
    .steps-header { text-align: center; margin-bottom: 3.5rem; }
    .steps-header h2 { font-size: 2.25rem; font-weight: 700; margin: 0 0 0.5rem; letter-spacing: -0.02em; }
    .steps-header p { color: var(--text-dim); font-size: 1.05rem; }
    .steps-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1.5rem; }
    @media (min-width: 961px) {
      #how .steps-grid { grid-template-columns: repeat(4, 1fr); }
    }
    .step-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 2.25rem;
      transition: var(--transition);
    }
    .step-card:hover {
      transform: translateY(-6px);
      border-color: var(--border-hover);
      box-shadow: 0 12px 32px rgba(0,0,0,0.25);
    }
    .step-num {
      width: 42px; height: 42px;
      background: rgba(16,185,129,0.1);
      color: var(--accent-light);
      border-radius: var(--radius-sm);
      display: flex; align-items: center; justify-content: center;
      font-weight: 700;
      font-size: 0.9rem;
      margin-bottom: 1.25rem;
    }
    .step-card h3 { font-size: 1.15rem; font-weight: 600; margin: 0 0 0.5rem; }
    .step-card p { font-size: 0.9rem; color: var(--text-muted); line-height: 1.55; margin: 0; }

    /* Testimonials */
    .testimonials {
      max-width: 1100px;
      margin: 0 auto;
      padding: 4rem 2rem;
      border-top: 1px solid var(--border);
      text-align: center;
    }
    .testimonials h2 { font-size: 1.75rem; font-weight: 700; margin-bottom: 0.5rem; }
    .testimonials-sub { color: var(--text-muted); font-size: 1rem; margin-bottom: 2.5rem; }
    .testimonial-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 1.25rem;
      text-align: left;
    }
    .testimonial-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1.75rem;
      display: flex;
      flex-direction: column;
      gap: 1rem;
      transition: var(--transition);
    }
    .testimonial-card:hover { border-color: var(--border-hover); transform: translateY(-2px); }
    .stars { color: #fbbf24; font-size: 1rem; letter-spacing: 2px; }
    .quote { font-size: 0.9rem; color: var(--text-muted); line-height: 1.7; flex: 1; font-style: italic; }
    .testimonial-author { display: flex; align-items: center; gap: 0.75rem; }
    .testimonial-author .avatar {
      width: 36px; height: 36px; border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      font-size: 0.7rem; font-weight: 700; color: #fff; flex-shrink: 0;
    }
    .testimonial-author strong { font-size: 0.85rem; color: var(--text); }
    .author-meta { font-size: 0.75rem; color: var(--text-dim); margin-top: 2px; }
    .trust-logos {
      margin-top: 2.5rem;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 0.5rem;
      flex-wrap: wrap;
    }
    .trust-logo-label { font-size: 0.8rem; color: var(--text-dim); margin-right: 0.25rem; }
    .trust-logo-name { font-size: 0.8rem; font-weight: 600; color: var(--text-muted); }
    .trust-logo-sep { color: var(--text-dim); font-size: 0.7rem; }

    /* SMS Section */
    .sms-section {
      max-width: 800px;
      margin: 0 auto;
      padding: 4rem 2rem;
      border-top: 1px solid var(--border);
    }
    .sms-section h2 { font-size: 1.75rem; font-weight: 700; margin-bottom: 1.5rem; text-align: center; }
    .sms-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 2rem;
    }
    .sms-card h3 { font-size: 1.1rem; font-weight: 600; margin-bottom: 1rem; color: var(--accent-light); }
    .sms-card ul { list-style: none; display: flex; flex-direction: column; gap: 0.75rem; }
    .sms-card li {
      font-size: 0.9rem;
      color: var(--text-muted);
      line-height: 1.5;
      padding-left: 1.25rem;
      position: relative;
    }
    .sms-card li::before { content: "\\2022"; position: absolute; left: 0; color: var(--accent); font-weight: 700; }
    .sms-card li strong { color: #e2e8f0; }
    .sms-contact {
      margin-top: 1.5rem;
      padding-top: 1.5rem;
      border-top: 1px solid var(--border);
      font-size: 0.9rem;
      color: var(--text-muted);
    }
    .sms-contact a { color: var(--accent-light); }
    .sms-contact a:hover { text-decoration: underline; }

    /* Footer */
    .footer {
      border-top: 1px solid var(--border);
      padding: 3rem 2rem;
      text-align: center;
    }
    .footer-links {
      display: flex;
      justify-content: center;
      gap: 1.5rem;
      margin-bottom: 1rem;
      flex-wrap: wrap;
    }
    .footer-links a { color: var(--text-dim); font-size: 0.85rem; font-weight: 500; transition: var(--transition); }
    .footer-links a:hover { color: var(--text); }
    .trust-badges {
      display: flex;
      justify-content: center;
      align-items: center;
      gap: 1.5rem;
      margin-bottom: 1.25rem;
      flex-wrap: wrap;
    }
    .trust-badge {
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      font-size: 0.78rem;
      font-weight: 600;
      color: var(--text-muted);
    }
    .trust-badge .trust-icon { font-size: 0.9rem; }
    .footer-copy { color: #475569; font-size: 0.8rem; }

    @media (max-width: 960px) {
      .hero { grid-template-columns: 1fr; padding: 3rem 1.5rem; gap: 2rem; }
      .hero h1 { font-size: 2.5rem; }
      .phone-wrap { display: none; }
      .steps-grid { grid-template-columns: 1fr; }
      .testimonial-grid { grid-template-columns: 1fr; }
      .nav-links { display: none; }
      .stats { gap: 1.5rem; }
    }
    @media (max-width: 480px) {
      .hero h1 { font-size: 2rem; }
      .input-row { flex-direction: column; }
      .input-btn { width: 100%; }
      .nav { padding: 1rem; }
    }
  </style>
</head>
<body>

  <nav class="nav">
    <div class="nav-left">
      <div class="nav-logo"><img src="/static/logo.svg" alt="TxtAnOffer"></div>
      <span>TxtAnOffer</span>
    </div>
    <div class="nav-links">
      <a href="#how">How it works</a>
      <a href="#trust">Accuracy</a>
      <a href="/pricing">Pricing</a>
      <a href="/demo">Demo</a>
      <a href="/faq">FAQ</a>
      <a href="/about">About</a>
      <a href="/login">Log In</a>
    </div>
    <a href="/signup" class="nav-cta">Start Free Trial</a>
  </nav>

  <section class="hero">
    <div class="hero-left">
      <div class="badge">Built for Texas REALTORS</div>
      <h1>
        Generate TREC contracts<br>
        <span class="gradient">by text message.</span>
      </h1>
      <p class="hero-sub">
        Text your offer from the parking lot. Get a filled <strong>TREC 20-19</strong> + <strong>Third Party Financing Addendum</strong> PDF in 10 seconds. No app download. No form filling. Just text and go.
      </p>

      <div class="input-card">
        <div class="input-label">Try it now &mdash; no signup required</div>
        <form id="live-demo-form">
          <div class="input-row">
            <input type="text" id="demo-input" placeholder="725k 3% 21day Harris 1234 Westheimer Rd" autocomplete="off">
            <button type="submit" class="input-btn">Generate &rarr;</button>
          </div>
        </form>
        <div class="input-hint">Type however feels natural — we handle messy texts. Just get the numbers in there.</div>
        <div class="demo-loading" id="demo-loading">Generating your contract...</div>
        <div class="demo-error" id="demo-error"></div>
        <div class="demo-result" id="demo-result">
          <div class="res-row"><span class="k">Address</span><span class="v" id="res-addr"></span></div>
          <div class="res-row"><span class="k">Price</span><span class="v" id="res-price"></span></div>
          <div class="res-row"><span class="k">Down payment</span><span class="v" id="res-down"></span></div>
          <div class="res-row"><span class="k">Closing</span><span class="v" id="res-close"></span></div>
          <a href="#" id="res-pdf" class="res-link" target="_blank">Download PDF &rarr;</a>
        </div>
        <div class="hero-phone">Prefer texting from your phone? <a href="sms:+18338970333">Text (833) 897-0333</a> to get started.</div>
      </div>

      <div class="stats">
        <div><div class="stat-num">&lt;10s</div><div class="stat-label">Generation time</div></div>
        <div><div class="stat-num">45 min</div><div class="stat-label">Saved per offer</div></div>
        <div><div class="stat-num">Free</div><div class="stat-label">No card required</div></div>
        <div><div class="stat-num">100%</div><div class="stat-label">Required fields checked</div></div>
      </div>

    </div>

    <div class="phone-wrap">
      <div class="phone-glow"></div>
      <div class="phone">
        <div class="phone-notch"></div>
        <div class="phone-screen">
          <div class="msg-time">Today 9:41 AM</div>
          <div class="msg-bubble msg-user">725k 3% 21day 123 Main St, Austin TX 78701</div>
          <div class="msg-bubble msg-bot">
            Your TREC contract is ready!<br><br>
            <strong style="color:#fff;">$725,000</strong><br>
            Close: <strong style="color:#fff;">Aug 12, 2026</strong><br><br>
            <a>txtanoffer.com/review/123-main-st.pdf</a>
          </div>
          <div class="pdf-preview">
            <div class="pdf-icon">PDF</div>
            <div style="min-width:0;overflow:hidden;">
              <div class="pdf-name">TREC_123_Main_St.pdf</div>
              <div class="pdf-meta">142 KB &middot; TREC 20-19 + 40-11</div>
            </div>
          </div>
          <div class="msg-time">2:15 PM</div>
          <div class="msg-bubble msg-bot msg-accepted">Listing agent accepted your offer on 123 Main St. &#9989;</div>
        </div>
      </div>
    </div>
  </section>

  <section class="steps" id="how">
    <div class="steps-header">
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
        <p>Generated from TREC's actual published 20-19 form &mdash; current Paragraph 12B commission language, the mandatory Water Disclosure, and the required IABS brokerage-services notice all included &mdash; not a stale template someone forgot to update. Mention an HOA and the 36-10 addendum attaches itself, checkbox and all &mdash; no separate form to remember.</p>
      </div>
      <div class="step-card">
        <div class="step-num">&check;</div>
        <h3>You review it. You send it.</h3>
        <p>TxtAnOffer drafts the contract; nothing goes to a buyer, seller, or listing agent until you look it over and decide it's ready.</p>
      </div>
    </div>
  </section>

  <section class="sms-section">
    <h2>SMS Messaging Details</h2>
    <div class="sms-card">
      <h3>How SMS Is Used</h3>
      <ul>
        <li><strong>Opt-in:</strong> Users sign up at txtanoffer.com/signup by providing their phone number and explicitly consenting to receive SMS messages.</li>
        <li><strong>Message frequency:</strong> Most messages are sent in direct response to user-initiated texts. We also send a one-time reminder a few days before the closing date of an offer you generated. We do not send marketing or promotional messages.</li>
        <li><strong>Message content:</strong> Replies contain contract confirmation details and a download link to the generated PDF; reminders reference the closing date of an offer already on file.</li>
        <li><strong>Sample message:</strong> <em>"Got it — $725,000, 3% down, closing Aug 13 2026. Your TREC contract is ready: txtanoffer.com/review/123-main-st.pdf — Reply STOP to unsubscribe, HELP for help. Msg&amp;data rates may apply."</em></li>
        <li><strong>Sample reminder message:</strong> <em>"Reminder: 123 Main St is scheduled to close on August 13, 2026 (3 days from now). Text DASHBOARD to review. Reply STOP to unsubscribe, HELP for help."</em></li>
        <li><strong>Opt-out:</strong> Reply STOP at any time to unsubscribe from all messages. Reply HELP for support.</li>
        <li><strong>Standard message and data rates may apply.</strong></li>
      </ul>
      <div class="sms-contact">
        Questions? Contact us at <a href="mailto:support@txtanoffer.com">support@txtanoffer.com</a>
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
      <a href="/pricing">Pricing</a>
      <a href="/playground">Parser Playground</a>
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
    fetch('/api/demo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({offer_text:text})})
    .then(function(r){return r.json()})
    .then(function(d){
      loading.style.display='none';
      if(d.error){errEl.textContent=d.error;errEl.style.display='block';return;}
      document.getElementById('res-addr').textContent=d.address;
      document.getElementById('res-price').textContent='$'+Number(d.price).toLocaleString();
      document.getElementById('res-down').textContent=d.down_pct+'%';
      document.getElementById('res-close').textContent=d.close_date;
      document.getElementById('res-pdf').href=d.pdf_url;
      result.classList.add('show');
    })
    .catch(function(){loading.style.display='none';errEl.textContent='Something went wrong. Try again.';errEl.style.display='block';});
  });
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
    reply = (
        f"DONE. TREC draft for {draft['address']} ready:\n\n"
        f"Review & Email: {pdf_url}\n\n"
        f"Includes: {includes}. DRAFT - Agent must review before signing.\n\n"
        f"Need to change price/terms? Just text new offer.\n\n"
        f"Reply DASHBOARD for all offers. STOP to unsubscribe, HELP for help."
        f"{status_line}"
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
    --bg: #0f172a;
    --bg-elevated: #1e293b;
    --bg-card: rgba(255,255,255,0.03);
    --border: rgba(255,255,255,0.06);
    --border-hover: rgba(16,185,129,0.3);
    --text: #f8fafc;
    --text-muted: #94a3b8;
    --text-dim: #64748b;
    --accent: #10b981;
    --accent-light: #34d399;
    --radius: 1.25rem;
    --radius-sm: 0.75rem;
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
    background:rgba(15,23,42,0.9);backdrop-filter:blur(16px);
    -webkit-backdrop-filter:blur(16px);
    border-bottom:1px solid var(--border);z-index:100;
  }}
  .nav-left {{display:flex;align-items:center;gap:0.6rem;font-weight:700;font-size:1.1rem;letter-spacing:-0.02em;}}
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
  .nav-cta:hover {{transform:scale(1.05);box-shadow:0 0 24px rgba(16,185,129,0.4);}}

  /* Page layout */
  .page {{max-width:580px;margin:0 auto;padding:4rem 1.5rem;overflow-x:hidden;width:100%;}}
  .page-badge {{
    display:inline-flex;align-items:center;gap:0.4rem;
    background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.2);
    color:var(--accent-light);font-size:0.7rem;font-weight:700;
    padding:0.35rem 0.85rem;border-radius:9999px;
    text-transform:uppercase;letter-spacing:0.06em;margin-bottom:1rem;
  }}
  .page h1 {{font-size:2.25rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.5rem;}}
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
  }}
  .wf-step {{text-align:center;flex:1;}}
  .wf-icon {{font-size:1.5rem;margin-bottom:0.4rem;}}
  .wf-title {{font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:var(--text);}}
  .wf-desc {{font-size:0.7rem;color:var(--text-dim);margin-top:0.2rem;line-height:1.4;}}
  .wf-arrow {{color:var(--accent);font-size:1.25rem;opacity:0.7;}}

  /* Card */
  .card {{
    background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
    padding:2rem;overflow:hidden;max-width:100%;
  }}
  .field-label {{
    font-size:0.7rem;font-weight:700;color:var(--text-dim);
    text-transform:uppercase;letter-spacing:0.07em;margin-bottom:0.5rem;display:block;
  }}
  .card input[type=text] {{
    width:100%;background:rgba(0,0,0,0.35);border:1px solid rgba(255,255,255,0.1);
    border-radius:var(--radius-sm);padding:0.8rem 1rem;color:var(--text);
    font-size:0.95rem;font-family:inherit;outline:none;transition:var(--transition);
  }}
  .card input[type=text]:focus {{border-color:var(--accent);box-shadow:0 0 0 3px rgba(16,185,129,0.15);}}
  .card input[type=text]::placeholder {{color:#475569;}}
  .card button {{
    width:100%;margin-top:0.75rem;
    background:linear-gradient(135deg,var(--accent),#059669);color:#fff;border:none;
    border-radius:var(--radius-sm);padding:0.85rem;font-weight:600;font-size:0.95rem;
    font-family:inherit;cursor:pointer;transition:var(--transition);
  }}
  .card button:hover {{transform:translateY(-2px);box-shadow:0 8px 24px rgba(16,185,129,0.35);}}
  .hint {{font-size:0.75rem;color:#475569;margin-top:0.5rem;}}

  /* Result */
  .result {{margin-top:1.5rem;padding-top:1.5rem;border-top:1px solid var(--border);}}
  .result-stamp {{
    display:inline-flex;align-items:center;gap:0.4rem;
    font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;
    color:var(--accent-light);background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.2);
    padding:0.3rem 0.7rem;border-radius:9999px;margin-bottom:1rem;
  }}
  .result-addr {{font-size:1.25rem;font-weight:700;color:var(--text);margin-bottom:1rem;}}
  .result-row {{display:flex;justify-content:space-between;padding:0.5rem 0;font-size:0.9rem;border-bottom:1px solid var(--border);}}
  .result-row .k {{color:var(--text-dim);font-size:0.8rem;text-transform:uppercase;letter-spacing:0.04em;font-weight:600;}}
  .result-row .v {{color:var(--text);font-weight:500;}}
  .result-ready {{font-size:0.85rem;color:var(--accent-light);margin-top:1rem;}}

  .pdf-preview {{margin-top:1.25rem;border:1px solid var(--border);border-radius:var(--radius-sm);overflow:hidden;max-width:100%;}}
  .pdf-preview-label {{
    font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;
    color:var(--text-dim);padding:0.6rem 1rem;background:rgba(255,255,255,0.03);
    border-bottom:1px solid var(--border);
  }}
  .pdf-frame {{width:100%;height:560px;border:none;background:#fff;}}
  .pdf-mobile {{display:none;padding:1.5rem;text-align:center;background:rgba(255,255,255,0.02);}}
  .pdf-mobile a {{color:var(--accent-light);font-weight:600;font-size:0.9rem;text-decoration:none;}}
  .pdf-mobile a:hover {{text-decoration:underline;}}
  @media(max-width:768px){{
    .pdf-frame {{display:none;}}
    .pdf-mobile {{display:block;}}
  }}

  .download-btn {{
    margin-top:1rem;display:block;text-align:center;
    background:linear-gradient(135deg,var(--accent),#059669);color:#fff;
    font-weight:600;font-size:0.9rem;padding:0.85rem;border-radius:var(--radius-sm);
    text-decoration:none;transition:var(--transition);
  }}
  .download-btn:hover {{transform:translateY(-2px);box-shadow:0 8px 24px rgba(16,185,129,0.35);}}
  .disclaimer {{margin-top:1rem;font-size:0.75rem;color:var(--text-dim);line-height:1.5;font-style:italic;}}

  /* Integration buttons */
  .integration-actions {{display:flex;gap:0.5rem;margin:1.25rem 0 0;flex-wrap:wrap;}}
  .int-btn {{
    flex:1;min-width:110px;padding:0.6rem 0.75rem;font-size:0.75rem;font-weight:600;
    border:1px solid var(--border);background:var(--bg-card);color:var(--text-muted);
    border-radius:var(--radius-sm);cursor:pointer;font-family:inherit;
    letter-spacing:0.02em;transition:var(--transition);
  }}
  .int-btn:hover {{border-color:var(--accent);color:var(--accent-light);}}

  /* Modals */
  .modal {{position:fixed;inset:0;background:rgba(15,23,42,0.9);display:flex;align-items:center;
    justify-content:center;z-index:1000;padding:20px;}}
  .modal-box {{
    background:var(--bg-elevated);padding:2rem;border-radius:var(--radius);width:100%;max-width:380px;
    position:relative;border:1px solid var(--border);
  }}
  .modal-title {{font-size:1.1rem;font-weight:700;color:var(--text);margin:0 0 1rem;}}
  .modal-desc {{font-size:0.85rem;color:var(--text-muted);margin:0 0 0.75rem;line-height:1.5;}}
  .modal-input {{
    width:100%;font-family:inherit;font-size:0.9rem;padding:0.7rem 0.85rem;
    border:1px solid rgba(255,255,255,0.1);background:rgba(0,0,0,0.3);color:var(--text);
    border-radius:var(--radius-sm);outline:none;margin-bottom:0.6rem;
  }}
  .modal-input:focus {{border-color:var(--accent);}}
  .modal-submit {{
    width:100%;padding:0.75rem;background:var(--accent);color:#fff;border:none;
    font-family:inherit;font-size:0.9rem;font-weight:600;border-radius:var(--radius-sm);cursor:pointer;
  }}
  .modal-submit:hover {{background:#059669;}}
  .modal-box .modal-close {{
    position:absolute;top:0.75rem;right:1rem;width:auto;margin-top:0;
    background:none;border:none;border-radius:0;padding:0;
    font-size:1.5rem;font-weight:400;line-height:1;color:var(--text-dim);cursor:pointer;
  }}
  .modal-box .modal-close:hover {{transform:none;box-shadow:none;color:var(--text);}}
  .modal-status {{margin-top:0.6rem;font-size:0.8rem;color:var(--text-dim);}}
  .modal-status.success {{color:var(--accent-light);}}
  .modal-status.fail {{color:#f87171;}}

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
    margin-top:1.25rem;padding:1rem;background:rgba(248,113,113,0.08);
    border:1px solid rgba(248,113,113,0.2);border-radius:var(--radius-sm);
    font-size:0.85rem;color:#f87171;
  }}
  .warning-note {{
    margin:1rem 0 0.75rem;padding:0.85rem 1rem;background:rgba(251,191,36,0.08);
    border:1px solid rgba(251,191,36,0.2);border-radius:var(--radius-sm);
    font-size:0.8rem;color:#fbbf24;line-height:1.5;
  }}
  .warning-note .wn-title {{font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.04em;margin-bottom:0.25rem;}}

  /* SMS command menu */
  .cmd-menu {{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:1.5rem;margin-top:1.5rem;}}
  .cmd-menu-title {{font-size:0.7rem;font-weight:700;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.06em;margin-bottom:1rem;}}
  .cmd-row {{display:flex;gap:1rem;padding:0.55rem 0;border-bottom:1px solid var(--border);align-items:baseline;}}
  .cmd-row:last-child {{border-bottom:none;}}
  .cmd-key {{font-family:monospace;color:var(--accent-light);font-size:0.85rem;flex:0 0 auto;white-space:nowrap;}}
  .cmd-desc {{color:var(--text-dim);font-size:0.85rem;}}
  @media(max-width:600px){{.cmd-row {{flex-direction:column;gap:0.15rem;}}}}

  /* Trust */
  .trust {{display:flex;gap:1.5rem;margin-top:2rem;justify-content:center;}}
  .trust-item {{text-align:center;}}
  .trust-val {{font-size:1.25rem;font-weight:800;color:var(--accent-light);}}
  .trust-label {{font-size:0.7rem;color:var(--text-dim);margin-top:0.2rem;font-weight:500;text-transform:uppercase;letter-spacing:0.04em;}}

  /* Footer */
  .foot {{text-align:center;margin-top:2rem;font-size:0.8rem;color:var(--text-dim);line-height:1.6;}}
  .foot a {{color:var(--accent-light);text-decoration:none;}}
  .foot a:hover {{text-decoration:underline;}}

  @media(max-width:600px){{
    .page {{padding:2rem 1rem;}}
    .page h1 {{font-size:1.75rem;}}
    .workflow {{flex-direction:column;gap:1rem;}}
    .wf-arrow {{transform:rotate(90deg);}}
    .nav-links {{display:none;}}
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
    <div class="nav-left">
      <div class="nav-logo"><img src="/static/logo.svg" alt="TxtAnOffer"></div>
      <span>TxtAnOffer</span>
    </div>
    <div class="nav-links">
      <a href="/">Home</a>
      <a href="/pricing">Pricing</a>
      <a href="/faq">FAQ</a>
      <a href="/login">Log In</a>
    </div>
    <a href="/signup" class="nav-cta">Start Free Trial</a>
  </nav>

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
                  body: JSON.stringify({{pdf_filename: filename, signer_email: email, signer_name: name, parsed: {parsed_json}}})
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
                  body: JSON.stringify({{source_id: 'demo-web', url: url}})
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
:root{--bg:#0f172a;--bg-card:rgba(255,255,255,0.03);--border:rgba(255,255,255,0.06);
--text:#f8fafc;--text-muted:#94a3b8;--text-dim:#64748b;--accent:#10b981;--accent-light:#34d399;
--radius:1.25rem;--radius-sm:0.75rem;}
*{margin:0;padding:0;box-sizing:border-box;}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;
-webkit-font-smoothing:antialiased;}
a{color:inherit;text-decoration:none;}
.nav{display:flex;align-items:center;justify-content:space-between;padding:1rem 2rem;
background:rgba(15,23,42,0.9);backdrop-filter:blur(16px);border-bottom:1px solid var(--border);
position:sticky;top:0;z-index:100;}
.nav-left{display:flex;align-items:center;gap:0.6rem;font-weight:700;font-size:1.1rem;}
.nav-logo{width:34px;height:34px;border-radius:22%;overflow:hidden;}
.nav-logo img{width:100%;height:100%;object-fit:contain;}
.nav-links{display:flex;gap:2rem;font-size:0.875rem;font-weight:500;color:var(--text-muted);}
.nav-links a:hover{color:var(--text);}
.nav-cta{background:var(--accent);color:#fff;padding:0.55rem 1.35rem;border-radius:9999px;
font-size:0.875rem;font-weight:600;}
.container{max-width:900px;margin:0 auto;padding:3rem 2rem;}
h1{font-size:2rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.5rem;}
.subtitle{color:var(--text-muted);font-size:1rem;margin-bottom:2rem;}
.playground-card{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:2rem;}
.input-area{margin-bottom:1.5rem;}
.input-area label{display:block;font-size:0.8rem;font-weight:600;color:var(--text-dim);
text-transform:uppercase;letter-spacing:0.05em;margin-bottom:0.5rem;}
.input-area textarea{width:100%;background:rgba(255,255,255,0.04);border:1px solid var(--border);
border-radius:var(--radius-sm);color:var(--text);font-family:inherit;font-size:1rem;
padding:1rem;resize:none;outline:none;transition:border 0.2s;}
.input-area textarea:focus{border-color:var(--accent);}
.parse-btn{background:linear-gradient(135deg,var(--accent),#059669);color:#fff;border:none;
padding:0.85rem 2rem;border-radius:var(--radius-sm);font-family:inherit;font-size:0.9rem;
font-weight:600;cursor:pointer;transition:all 0.2s;}
.parse-btn:hover{transform:translateY(-2px);box-shadow:0 8px 24px rgba(16,185,129,0.3);}
.result{margin-top:1.5rem;display:none;}
.result.show{display:block;}
.result-grid{display:grid;grid-template-columns:1fr 1fr;gap:0.75rem;}
.result-item{background:rgba(255,255,255,0.025);border:1px solid rgba(255,255,255,0.05);
border-radius:var(--radius-sm);padding:1rem;}
.result-label{font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;
color:var(--text-dim);margin-bottom:0.25rem;}
.result-value{font-size:1.1rem;font-weight:700;color:var(--text);}
.result-value.accent{color:var(--accent-light);}
.error-msg{background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.2);
border-radius:var(--radius-sm);padding:1rem;color:#fca5a5;font-size:0.9rem;margin-top:1rem;display:none;}
.error-msg.show{display:block;}
.warn-msg{background:rgba(245,158,11,0.1);border:1px solid rgba(245,158,11,0.2);
border-radius:var(--radius-sm);padding:1rem;color:#fcd34d;font-size:0.9rem;margin-bottom:1rem;display:none;}
.warn-msg.show{display:block;}
.examples{margin-top:2rem;}
.examples h3{font-size:0.9rem;font-weight:700;margin-bottom:1rem;color:var(--text-muted);}
.example-chips{display:flex;flex-wrap:wrap;gap:0.5rem;}
.chip{background:rgba(255,255,255,0.04);border:1px solid var(--border);border-radius:9999px;
padding:0.4rem 0.85rem;font-size:0.8rem;color:var(--text-muted);cursor:pointer;transition:all 0.2s;}
.chip:hover{border-color:var(--accent);color:var(--accent-light);}
.formats{margin-top:2.5rem;padding-top:2rem;border-top:1px solid var(--border);}
.formats h3{font-size:1rem;font-weight:700;margin-bottom:1rem;}
.format-grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem;}
.format-item{font-size:0.85rem;color:var(--text-muted);line-height:1.6;}
.format-item strong{color:var(--text);font-weight:600;}
@media(max-width:600px){
.result-grid{grid-template-columns:1fr;}
.format-grid{grid-template-columns:1fr;}
.nav-links{display:none;}
}
</style>
</head>
<body>
<nav class="nav">
<a href="/" class="nav-left">
<div class="nav-logo"><img src="/static/logo.svg" alt="TxtAnOffer"></div>
<span>TxtAnOffer</span>
</a>
<div class="nav-links">
<a href="/">Home</a>
<a href="/demo">Demo</a>
<a href="/pricing">Pricing</a>
<a href="/faq">FAQ</a>
</div>
<a href="/signup" class="nav-cta">Start Free Trial</a>
</nav>

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
    auth_error = require_api_auth()
    if auth_error:
        return auth_error

    if request.method == "GET":
        source_id = request.args.get("source_id", "")
        if not source_id:
            return jsonify({"error": "source_id required"}), 400
        url = get_webhook(source_id)
        return jsonify({"source_id": source_id, "url": url, "active": url is not None})

    if request.method == "POST":
        data = request.get_json()
        if not data:
            return jsonify({"error": "JSON body required"}), 400
        source_id = data.get("source_id", "")
        url = data.get("url", "")
        if not source_id or not url:
            return jsonify({"error": "source_id and url required"}), 400
        if not _is_safe_webhook_url(url):
            return jsonify({"error": "Invalid webhook URL (must be public HTTPS)"}), 400
        save_webhook(source_id, url)
        track_event("webhook_configured", source_id, {"url": url})
        return jsonify({"success": True, "source_id": source_id, "url": url})

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
    auth_error = require_api_auth()
    if auth_error:
        return auth_error

    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "JSON body required"}), 400

    pdf_filename = data.get("pdf_filename", "")
    parsed = data.get("parsed", {})
    signer_email = data.get("signer_email", "")
    signer_name = data.get("signer_name", "")

    if not pdf_filename or not signer_email or not signer_name:
        return jsonify({"success": False, "error": "pdf_filename, signer_email, and signer_name required"}), 400

    pdf_path = os.path.join(OUTPUT_DIR, pdf_filename)
    if not os.path.exists(pdf_path):
        return jsonify({"success": False, "error": "PDF not found"}), 404

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
    --bg: #0f172a;
    --bg-elevated: #1e293b;
    --bg-card: rgba(255,255,255,0.03);
    --border: rgba(255,255,255,0.06);
    --border-hover: rgba(16,185,129,0.3);
    --text: #f8fafc;
    --text-muted: #94a3b8;
    --text-dim: #64748b;
    --accent: #10b981;
    --accent-light: #34d399;
    --radius: 1.25rem;
    --radius-sm: 0.75rem;
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
    background:rgba(15,23,42,0.9);backdrop-filter:blur(16px);
    -webkit-backdrop-filter:blur(16px);
    border-bottom:1px solid var(--border);z-index:100;
  }
  .nav-left {display:flex;align-items:center;gap:0.6rem;font-weight:700;font-size:1.1rem;letter-spacing:-0.02em;}
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
  .nav-cta:hover {transform:scale(1.05);box-shadow:0 0 24px rgba(16,185,129,0.4);}

  /* Header */
  .page-header {text-align:center;padding:4rem 2rem 3rem;max-width:700px;margin:0 auto;}
  .page-header h1 {font-size:2.75rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.75rem;}
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
  }
  .pricing-card:hover {transform:translateY(-4px);border-color:var(--border-hover);}
  .pricing-card.featured {border-color:var(--accent);position:relative;}
  .featured-badge {
    position:absolute;top:-0.75rem;left:50%;transform:translateX(-50%);
    font-size:0.65rem;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;
    color:var(--accent-light);background:var(--bg);
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
  .check {color:var(--accent-light);font-weight:700;font-size:0.9rem;}

  .cta-btn {
    display:block;width:100%;padding:0.85rem;
    background:linear-gradient(135deg,var(--accent),#059669);color:#fff;
    border:none;font-family:inherit;font-size:0.9rem;font-weight:600;
    border-radius:var(--radius-sm);cursor:pointer;text-align:center;
    transition:var(--transition);text-decoration:none;
  }
  .cta-btn:hover {transform:translateY(-2px);box-shadow:0 8px 24px rgba(16,185,129,0.35);}
  .cta-btn.outline {
    background:transparent;border:1px solid var(--border);color:var(--text-muted);
  }
  .cta-btn.outline:hover {border-color:var(--accent);color:var(--accent-light);transform:translateY(-2px);}

  /* Value Props */
  .value-section {max-width:1100px;margin:0 auto;padding:3rem 2rem;border-top:1px solid var(--border);}
  .value-grid {display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:1.25rem;}
  .value-card {
    background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
    padding:1.75rem;
  }
  .value-title {
    font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;
    color:var(--accent-light);margin-bottom:0.5rem;
  }
  .value-text {color:var(--text-muted);font-size:0.9rem;line-height:1.6;}

  /* Footer */
  .footer-note {text-align:center;padding:2rem;font-size:0.8rem;color:var(--text-dim);}
  .footer-note a {color:var(--accent-light);}
  .footer-note a:hover {text-decoration:underline;}

  @media(max-width:600px) {
    .page-header h1 {font-size:2rem;}
    .pricing-grid {padding:0 1rem 2rem;}
    .nav-links {display:none;}
  }
</style>
</head>
<body>

<nav class="nav">
  <a href="/" class="nav-left">
    <div class="nav-logo"><img src="/static/logo.svg" alt="TxtAnOffer"></div>
    <span>TxtAnOffer</span>
  </a>
  <div class="nav-links">
    <a href="/">Home</a>
    <a href="/demo">Demo</a>
    <a href="/faq">FAQ</a>
    <a href="/login">Log In</a>
  </div>
  <a href="/signup" class="nav-cta">Start Free Trial</a>
</nav>

<div class="page-header">
  <h1>Simple pricing.<br><span class="gradient">Massive time savings.</span></h1>
  <p>Stop spending 45 minutes per offer. Pick a plan and start generating contracts in seconds.</p>
  <p style="margin-top:1rem;color:var(--accent-light);font-weight:600;font-size:0.95rem;">Try free — 3 offers, no card required.</p>
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
    <p class="plan-desc">Close deals faster with one-click signing and CRM sync.</p>
    <div class="price-row">
      <span class="price-current">$79</span>
      <span class="price-period">/month</span>
    </div>
    <ul class="features">
      <li><span class="check">&#10003;</span> Everything in Starter</li>
      <li><span class="check">&#10003;</span> One-click DocuSign send</li>
      <li><span class="check">&#10003;</span> Webhook automation (Zapier-compatible)</li>
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
  <div style="background:rgba(16,185,129,0.06);border:1px solid rgba(16,185,129,0.15);border-radius:1rem;padding:2rem 1.75rem;">
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
  :root{{--bg:#0f172a;--bg-card:rgba(255,255,255,0.03);--border:rgba(255,255,255,0.06);
    --text:#f8fafc;--text-muted:#94a3b8;--text-dim:#64748b;
    --accent:#10b981;--accent-light:#34d399;--radius:1.25rem;--radius-sm:0.75rem;}}
  *{{margin:0;padding:0;box-sizing:border-box;}}
  body{{background:var(--bg);min-height:100vh;margin:0;display:flex;align-items:center;
    justify-content:center;padding:2rem;font-family:'Inter',-apple-system,sans-serif;color:var(--text);}}
  .card{{background:var(--bg-card);border:1px solid var(--border);padding:3rem;border-radius:var(--radius);
    max-width:520px;width:100%;text-align:center;}}
  h1{{font-size:2rem;font-weight:800;margin:0 0 0.75rem;letter-spacing:-0.02em;}}
  .sub{{color:var(--text-muted);font-size:1rem;line-height:1.6;margin-bottom:1.5rem;}}
  .next-steps{{text-align:left;background:rgba(255,255,255,0.02);border:1px solid var(--border);
    padding:1.5rem;border-radius:var(--radius-sm);margin-bottom:1.5rem;}}
  .next-steps h3{{font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;
    color:var(--accent-light);margin:0 0 0.75rem;}}
  .next-steps ol{{margin:0;padding-left:1.25rem;}}
  .next-steps li{{margin:0.5rem 0;font-size:0.9rem;color:var(--text-muted);line-height:1.5;}}
  .next-steps li strong{{color:var(--text);}}
  .btn{{display:inline-block;padding:0.85rem 2rem;
    background:linear-gradient(135deg,var(--accent),#059669);color:#fff;
    text-decoration:none;border-radius:var(--radius-sm);font-weight:600;font-size:0.95rem;
    transition:all 0.2s ease;}}
  .btn:hover{{transform:translateY(-2px);box-shadow:0 8px 24px rgba(16,185,129,0.35);}}
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

        # Activate subscription on agent's phone number
        if customer_phone:
            user = get_user(customer_phone)
            if not user:
                create_user(customer_phone)
            activate_subscription(customer_phone, customer_id, subscription_id)

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
                    'background:linear-gradient(135deg,var(--accent),#059669);color:#fff;border-radius:var(--radius-sm);'
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
  :root{{--bg:#0f172a;--bg-card:rgba(255,255,255,0.03);--border:rgba(255,255,255,0.06);
    --text:#f8fafc;--text-muted:#94a3b8;--text-dim:#64748b;
    --accent:#10b981;--accent-light:#34d399;--radius:1.25rem;--radius-sm:0.75rem;
    --transition:all 0.2s ease;}}
  *{{margin:0;padding:0;box-sizing:border-box;}}
  body{{background:var(--bg);min-height:100vh;margin:0;display:flex;align-items:center;
    justify-content:center;padding:2rem;font-family:'Inter',-apple-system,sans-serif;color:var(--text);}}
  a{{color:inherit;text-decoration:none;}}
  .wrap{{width:100%;max-width:460px;}}
  .nav-back{{display:flex;align-items:center;gap:0.5rem;margin-bottom:1.5rem;}}
  .nav-back img{{width:28px;height:28px;border-radius:22%;object-fit:contain;}}
  .nav-back span{{font-size:0.85rem;color:var(--text-muted);}}
  .nav-back:hover span{{color:var(--text);}}
  h1{{font-size:1.75rem;font-weight:800;letter-spacing:-0.02em;margin-bottom:0.5rem;}}
  .sub{{color:var(--text-muted);font-size:0.95rem;line-height:1.6;margin-bottom:1.5rem;}}
  .card{{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:1.75rem;}}
  .field-label{{font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;
    color:var(--text-dim);margin-bottom:0.4rem;display:block;}}
  input[type=text],input[type=tel],input[type=email]{{
    width:100%;background:rgba(0,0,0,0.35);border:1px solid rgba(255,255,255,0.1);
    border-radius:var(--radius-sm);padding:0.75rem 1rem;color:var(--text);
    font-size:0.95rem;font-family:inherit;outline:none;margin-bottom:1rem;transition:var(--transition);
  }}
  input:focus{{border-color:var(--accent);box-shadow:0 0 0 3px rgba(16,185,129,0.15);}}
  input::placeholder{{color:#475569;}}
  .consent-row{{
    display:flex;align-items:flex-start;gap:0.75rem;margin:1rem 0;padding:1rem;
    background:rgba(16,185,129,0.05);border:1px solid rgba(16,185,129,0.15);border-radius:var(--radius-sm);
  }}
  .consent-row input[type=checkbox]{{margin-top:0.2rem;width:18px;height:18px;flex-shrink:0;accent-color:var(--accent);}}
  .consent-row label{{font-size:0.8rem;line-height:1.6;color:var(--text-muted);}}
  .consent-row a{{color:var(--accent-light);text-decoration:underline;}}
  button{{
    width:100%;margin-top:0.75rem;
    background:linear-gradient(135deg,var(--accent),#059669);color:#fff;border:none;
    padding:0.85rem;font-family:inherit;font-size:0.95rem;font-weight:600;
    border-radius:var(--radius-sm);cursor:pointer;transition:var(--transition);
  }}
  button:hover{{transform:translateY(-2px);box-shadow:0 8px 24px rgba(16,185,129,0.35);}}
  button:disabled{{opacity:0.4;cursor:not-allowed;transform:none;box-shadow:none;}}
  .success{{
    margin-top:1rem;padding:1rem;background:rgba(16,185,129,0.08);
    border:1px solid rgba(16,185,129,0.2);border-radius:var(--radius-sm);
    font-size:0.9rem;color:var(--accent-light);text-align:center;
  }}
  .error{{
    margin-top:1rem;padding:1rem;background:rgba(239,68,68,0.08);
    border:1px solid rgba(239,68,68,0.2);border-radius:var(--radius-sm);
    font-size:0.9rem;color:#fca5a5;text-align:center;
  }}
  .foot{{text-align:center;margin-top:1.5rem;font-size:0.8rem;color:var(--text-dim);}}
  .foot a{{color:var(--accent-light);text-decoration:none;}}
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
  :root{{--bg:#0f172a;--bg-card:rgba(255,255,255,0.03);--border:rgba(255,255,255,0.06);
    --text:#f8fafc;--text-muted:#94a3b8;--text-dim:#64748b;
    --accent:#10b981;--accent-light:#34d399;--radius:1.25rem;--radius-sm:0.75rem;
    --transition:all 0.2s ease;}}
  *{{margin:0;padding:0;box-sizing:border-box;}}
  body{{background:var(--bg);min-height:100vh;margin:0;display:flex;align-items:center;
    justify-content:center;padding:2rem;font-family:'Inter',-apple-system,sans-serif;color:var(--text);}}
  a{{color:inherit;text-decoration:none;}}
  .wrap{{width:100%;max-width:400px;}}
  .nav-back{{display:flex;align-items:center;gap:0.5rem;margin-bottom:1.5rem;}}
  .nav-back img{{width:28px;height:28px;border-radius:22%;object-fit:contain;}}
  .nav-back span{{font-size:0.85rem;color:var(--text-muted);}}
  .nav-back:hover span{{color:var(--text);}}
  h1{{font-size:1.75rem;font-weight:800;letter-spacing:-0.02em;margin-bottom:0.5rem;}}
  .sub{{color:var(--text-muted);font-size:0.95rem;margin-bottom:1.5rem;line-height:1.5;}}
  .card{{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:1.75rem;}}
  label{{font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;
    color:var(--text-dim);display:block;margin-bottom:0.4rem;}}
  input{{
    width:100%;background:rgba(0,0,0,0.35);border:1px solid rgba(255,255,255,0.1);
    border-radius:var(--radius-sm);padding:0.75rem 1rem;color:var(--text);
    font-size:0.95rem;font-family:inherit;outline:none;transition:var(--transition);
  }}
  input:focus{{border-color:var(--accent);box-shadow:0 0 0 3px rgba(16,185,129,0.15);}}
  input::placeholder{{color:#475569;}}
  .sms-note{{font-size:0.8rem;color:var(--text-dim);margin:0.75rem 0 0;line-height:1.5;}}
  button{{
    width:100%;margin-top:1rem;
    background:linear-gradient(135deg,var(--accent),#059669);color:#fff;border:none;
    padding:0.85rem;font-family:inherit;font-size:0.95rem;font-weight:600;
    border-radius:var(--radius-sm);cursor:pointer;transition:var(--transition);
  }}
  button:hover{{transform:translateY(-2px);box-shadow:0 8px 24px rgba(16,185,129,0.35);}}
  .msg{{margin-top:1rem;padding:0.85rem;border-radius:var(--radius-sm);font-size:0.9rem;text-align:center;}}
  .msg.success{{background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.2);color:var(--accent-light);}}
  .msg.error{{background:rgba(248,113,113,0.08);border:1px solid rgba(248,113,113,0.2);color:#f87171;}}
  .msg a{{color:var(--accent-light);}}
  .alt{{text-align:center;margin-top:1.25rem;font-size:0.85rem;color:var(--text-dim);}}
  .alt a{{color:var(--accent-light);text-decoration:none;}}
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
    --bg: #0f172a;
    --bg-elevated: #1e293b;
    --bg-card: rgba(255,255,255,0.03);
    --border: rgba(255,255,255,0.06);
    --border-hover: rgba(16,185,129,0.3);
    --text: #f8fafc;
    --text-muted: #94a3b8;
    --text-dim: #64748b;
    --accent: #10b981;
    --accent-light: #34d399;
    --radius: 1.25rem;
    --radius-sm: 0.75rem;
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
    background:rgba(15,23,42,0.9);backdrop-filter:blur(16px);
    -webkit-backdrop-filter:blur(16px);
    border-bottom:1px solid var(--border);z-index:100;
  }
  .nav-left {display:flex;align-items:center;gap:0.6rem;font-weight:700;font-size:1.1rem;letter-spacing:-0.02em;}
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
  .nav-cta:hover {transform:scale(1.05);box-shadow:0 0 24px rgba(16,185,129,0.4);}

  .container {max-width:720px;margin:0 auto;padding:3rem 2rem 4rem;}
  .page-header {margin-bottom:2.5rem;}
  .page-header h1 {font-size:2rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.25rem;}
  .page-header .updated {font-size:0.8rem;color:var(--text-dim);}

  .legal-card {
    background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
    padding:2.5rem 2rem;
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
    background:rgba(16,185,129,0.05);border-left:3px solid var(--accent);
    padding:1rem 1.25rem;margin:1rem 0;border-radius:0 var(--radius-sm) var(--radius-sm) 0;
    font-size:0.85rem;color:var(--text);line-height:1.7;
  }
  .section-num {color:var(--accent-light);font-weight:700;margin-right:0.25rem;}
  .foot {text-align:center;margin-top:2rem;font-size:0.8rem;color:var(--text-dim);}
  .foot a {color:var(--accent-light);}
  .foot a:hover {text-decoration:underline;}

  @media(max-width:600px) {
    .container {padding:2rem 1rem 3rem;}
    .legal-card {padding:1.5rem 1.25rem;}
    .nav-links {display:none;}
  }
</style>
</head>
<body>
<nav class="nav">
  <a href="/" class="nav-left">
    <div class="nav-logo"><img src="/static/logo.svg" alt="TxtAnOffer"></div>
    <span>TxtAnOffer</span>
  </a>
  <div class="nav-links">
    <a href="/">Home</a>
    <a href="/demo">Demo</a>
    <a href="/pricing">Pricing</a>
    <a href="/faq">FAQ</a>
  </div>
  <a href="/signup" class="nav-cta">Start Free Trial</a>
</nav>

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
    --bg: #0f172a;
    --bg-elevated: #1e293b;
    --bg-card: rgba(255,255,255,0.03);
    --border: rgba(255,255,255,0.06);
    --border-hover: rgba(16,185,129,0.3);
    --text: #f8fafc;
    --text-muted: #94a3b8;
    --text-dim: #64748b;
    --accent: #10b981;
    --accent-light: #34d399;
    --radius: 1.25rem;
    --radius-sm: 0.75rem;
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
    background:rgba(15,23,42,0.9);backdrop-filter:blur(16px);
    -webkit-backdrop-filter:blur(16px);
    border-bottom:1px solid var(--border);z-index:100;
  }
  .nav-left {display:flex;align-items:center;gap:0.6rem;font-weight:700;font-size:1.1rem;letter-spacing:-0.02em;}
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
  .nav-cta:hover {transform:scale(1.05);box-shadow:0 0 24px rgba(16,185,129,0.4);}

  .container {max-width:720px;margin:0 auto;padding:3rem 2rem 4rem;}
  .page-header {margin-bottom:2.5rem;}
  .page-header h1 {font-size:2rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.25rem;}
  .page-header .updated {font-size:0.8rem;color:var(--text-dim);}

  .legal-card {
    background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
    padding:2.5rem 2rem;
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
  .foot a {color:var(--accent-light);}
  .foot a:hover {text-decoration:underline;}

  @media(max-width:600px) {
    .container {padding:2rem 1rem 3rem;}
    .legal-card {padding:1.5rem 1.25rem;}
    .nav-links {display:none;}
  }
</style>
</head>
<body>
<nav class="nav">
  <a href="/" class="nav-left">
    <div class="nav-logo"><img src="/static/logo.svg" alt="TxtAnOffer"></div>
    <span>TxtAnOffer</span>
  </a>
  <div class="nav-links">
    <a href="/">Home</a>
    <a href="/demo">Demo</a>
    <a href="/pricing">Pricing</a>
    <a href="/faq">FAQ</a>
  </div>
  <a href="/signup" class="nav-cta">Start Free Trial</a>
</nav>

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

    <h2>3. SMS Messaging</h2>
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
    --bg: #0f172a;
    --bg-card: rgba(255,255,255,0.03);
    --border: rgba(255,255,255,0.06);
    --text: #f8fafc;
    --text-muted: #94a3b8;
    --text-dim: #64748b;
    --accent: #10b981;
    --accent-light: #34d399;
    --radius: 1.25rem;
    --radius-sm: 0.75rem;
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
    background:rgba(15,23,42,0.9);backdrop-filter:blur(16px);
    -webkit-backdrop-filter:blur(16px);
    border-bottom:1px solid var(--border);z-index:100;
  }
  .nav-left {display:flex;align-items:center;gap:0.6rem;font-weight:700;font-size:1.1rem;letter-spacing:-0.02em;}
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
  .nav-cta:hover {transform:scale(1.05);box-shadow:0 0 24px rgba(16,185,129,0.4);}
  .container {max-width:720px;margin:0 auto;padding:3rem 2rem 4rem;}
  .page-header {margin-bottom:2.5rem;}
  .page-header h1 {font-size:2rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.25rem;}
  .page-header p {font-size:0.9rem;color:var(--text-muted);}
  .faq-item {
    background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
    padding:1.5rem 1.75rem;margin-bottom:1rem;
  }
  .faq-item h2 {font-size:1rem;font-weight:700;margin-bottom:0.6rem;}
  .faq-item p {font-size:0.85rem;line-height:1.75;color:var(--text-muted);}
  .faq-item p + p {margin-top:0.5rem;}
  .faq-item strong {color:var(--text);font-weight:600;}
  .foot {text-align:center;margin-top:2rem;font-size:0.8rem;color:var(--text-dim);}
  .foot a {color:var(--accent-light);}
  .foot a:hover {text-decoration:underline;}
  @media(max-width:600px) {
    .container {padding:2rem 1rem 3rem;}
    .faq-item {padding:1.25rem 1.25rem;}
    .nav-links {display:none;}
  }
</style>
</head>
<body>
<nav class="nav">
  <a href="/" class="nav-left">
    <div class="nav-logo"><img src="/static/logo.svg" alt="TxtAnOffer"></div>
    <span>TxtAnOffer</span>
  </a>
  <div class="nav-links">
    <a href="/">Home</a>
    <a href="/demo">Demo</a>
    <a href="/pricing">Pricing</a>
  </div>
  <a href="/signup" class="nav-cta">Start Free Trial</a>
</nav>

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
    <p>Yes &mdash; text <strong>AMEND &lt;address&gt; price &lt;value&gt;</strong> or <strong>AMEND &lt;address&gt; close +&lt;days&gt;</strong> (e.g. <em>"AMEND 123 Main St price 730k"</em> or <em>"AMEND 123 Main St close +10"</em>) and you'll get back a filled TREC 39-11 Amendment for that contract. It's included on every plan, works the same way in the <a href="/demo" style="color:var(--accent-light);">web demo</a>, and shows up nested under the original offer on your <strong>Dashboard</strong>. Only the price or closing-date field you asked to change is filled &mdash; everything else on the form is left blank for you to complete, same as the main contract.</p>
  </div>

  <div class="faq-item">
    <h2>Do you store my texts or offers?</h2>
    <p>Generated PDFs are stored temporarily for download and deleted after 30 days. SMS logs are retained for 90 days for support and debugging. We do not sell or share your data. See our <a href="/privacy" style="color:var(--accent-light);">Privacy Policy</a> for the full breakdown.</p>
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
    <p>You'll receive a confirmation reply for every offer received, generally within seconds. If you don't get one within 30 seconds, try again or use the <a href="/demo" style="color:var(--accent-light);">web interface</a> at txtanoffer.com/demo.</p>
  </div>

  <div class="faq-item">
    <h2>Do I need E&amp;O insurance to use TxtAnOffer?</h2>
    <p>TxtAnOffer does not carry Errors &amp; Omissions insurance. Any E&amp;O coverage applicable to a transaction is your own policy as a licensed agent &mdash; it's your responsibility to review and stand behind every document you present or sign. See <a href="/terms" style="color:var(--accent-light);">Terms of Service</a> for details.</p>
  </div>

  <p class="foot">Still have a question? Email <a href="mailto:support@txtanoffer.com">support@txtanoffer.com</a>.<br><a href="/">&larr; Back to home</a> &middot; <a href="/terms">Terms</a> &middot; <a href="/privacy">Privacy Policy</a></p>
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
<meta name="description" content="TxtAnOffer was built by a licensed Texas REALTOR who was tired of losing deals to 45-minute paperwork. Here's why it exists.">
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
  :root {
    --bg: #0f172a;
    --bg-card: rgba(255,255,255,0.03);
    --border: rgba(255,255,255,0.06);
    --text: #f8fafc;
    --text-muted: #94a3b8;
    --text-dim: #64748b;
    --accent: #10b981;
    --accent-light: #34d399;
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
    background:rgba(15,23,42,0.9);backdrop-filter:blur(16px);
    -webkit-backdrop-filter:blur(16px);
    border-bottom:1px solid var(--border);z-index:100;
  }
  .nav-left {display:flex;align-items:center;gap:0.6rem;font-weight:700;font-size:1.1rem;letter-spacing:-0.02em;}
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
  .nav-cta:hover {transform:scale(1.05);box-shadow:0 0 24px rgba(16,185,129,0.4);}
  .container {max-width:680px;margin:0 auto;padding:3.5rem 2rem 4rem;}
  .avatar-lg {
    width:64px;height:64px;border-radius:50%;overflow:hidden;margin-bottom:1.5rem;
    border:2px solid var(--border);
  }
  .avatar-lg img {width:100%;height:100%;object-fit:cover;}
  h1 {font-size:2.1rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.4rem;}
  .kicker {font-size:0.9rem;color:var(--accent-light);font-weight:600;margin-bottom:1.75rem;}
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
  .foot a {color:var(--accent-light);}
  .foot a:hover {text-decoration:underline;}
  @media(max-width:600px) {
    .container {padding:2.5rem 1.25rem 3rem;}
    .nav-links {display:none;}
  }
</style>
</head>
<body>
<nav class="nav">
  <a href="/" class="nav-left">
    <div class="nav-logo"><img src="/static/logo.svg" alt="TxtAnOffer"></div>
    <span>TxtAnOffer</span>
  </a>
  <div class="nav-links">
    <a href="/">Home</a>
    <a href="/demo">Demo</a>
    <a href="/faq">FAQ</a>
    <a href="/contact">Contact</a>
  </div>
  <a href="/signup" class="nav-cta">Start Free Trial</a>
</nav>

<div class="container">
  <div class="avatar-lg"><img src="/static/logo.svg" alt="Phanel Jean Baptiste"></div>
  <h1>Built by a Texas Agent, for Texas Agents</h1>
  <div class="kicker">The story behind TxtAnOffer</div>

  <div class="about-body">
    <p>Hi, I'm <strong>Phanel Jean Baptiste</strong>, a licensed Texas REALTOR and the founder of TxtAnOffer.</p>

    <p>I built this tool because I was tired of losing deals while sitting in my car filling out TREC forms. In Texas real estate, speed wins &mdash; the agent who gets their offer in first often gets the house. But pulling out a laptop in a parking lot, opening ZipForm, and manually filling 40+ fields on a TREC 20-19 takes 45 minutes.</p>

    <p>That 45 minutes costs deals.</p>

    <p>TxtAnOffer cuts that to 10 seconds. Text the details from your phone. Get a filled PDF. Review and sign. Done.</p>

    <h2>Why I Care</h2>
    <p>I'm not a Silicon Valley startup. I'm an agent who saw a problem and built the solution. Every feature in TxtAnOffer was designed because I needed it in the field:</p>
    <ul>
      <li><strong>SMS-first</strong> because I'm always on my phone</li>
      <li><strong>Auto-filled TREC forms</strong> because I hate blank fields</li>
      <li><strong>Email delivery</strong> because the listing agent needs it now</li>
      <li><strong>Draft warnings</strong> because I never want to send something I haven't reviewed</li>
    </ul>

    <h2>The Mission</h2>
    <p>Give every Texas agent the tools to compete with the big teams. You don't need an admin, a laptop, or 45 minutes. You need your phone and 10 seconds.</p>
  </div>

  <div class="signoff">
    <strong>Phanel Jean Baptiste</strong>
    TREC License #000137 &middot; RE/MAX<br>
    <a href="mailto:pejeanbaptiste@gmail.com" style="color:var(--accent-light);">pejeanbaptiste@gmail.com</a>
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
    --bg: #0f172a;
    --bg-card: rgba(255,255,255,0.03);
    --border: rgba(255,255,255,0.06);
    --text: #f8fafc;
    --text-muted: #94a3b8;
    --text-dim: #64748b;
    --accent: #10b981;
    --accent-light: #34d399;
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
    background:rgba(15,23,42,0.9);backdrop-filter:blur(16px);
    -webkit-backdrop-filter:blur(16px);
    border-bottom:1px solid var(--border);z-index:100;
  }
  .nav-left {display:flex;align-items:center;gap:0.6rem;font-weight:700;font-size:1.1rem;letter-spacing:-0.02em;}
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
  .nav-cta:hover {transform:scale(1.05);box-shadow:0 0 24px rgba(16,185,129,0.4);}
  .container {max-width:560px;margin:0 auto;padding:3.5rem 2rem 4rem;}
  .page-header {margin-bottom:2rem;}
  .page-header h1 {font-size:2rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.4rem;}
  .page-header p {font-size:0.9rem;color:var(--text-muted);}
  .contact-card {
    background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
    padding:1.75rem;margin-bottom:1rem;display:flex;align-items:center;gap:1rem;
    transition:var(--transition);
  }
  a.contact-card:hover {border-color:rgba(16,185,129,0.3);transform:translateY(-1px);}
  .contact-icon {
    width:44px;height:44px;border-radius:50%;background:rgba(16,185,129,0.12);
    display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:1.2rem;
  }
  .contact-label {font-size:0.75rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.04em;margin-bottom:0.15rem;}
  .contact-value {font-size:1rem;font-weight:600;color:var(--text);}
  .foot {text-align:center;margin-top:2rem;font-size:0.8rem;color:var(--text-dim);}
  .foot a {color:var(--accent-light);}
  .foot a:hover {text-decoration:underline;}
  @media(max-width:600px) {
    .container {padding:2.5rem 1.25rem 3rem;}
    .nav-links {display:none;}
  }
</style>
</head>
<body>
<nav class="nav">
  <a href="/" class="nav-left">
    <div class="nav-logo"><img src="/static/logo.svg" alt="TxtAnOffer"></div>
    <span>TxtAnOffer</span>
  </a>
  <div class="nav-links">
    <a href="/">Home</a>
    <a href="/demo">Demo</a>
    <a href="/faq">FAQ</a>
    <a href="/about">About</a>
  </div>
  <a href="/signup" class="nav-cta">Start Free Trial</a>
</nav>

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
                "default_earnest_pct": float(request.form.get("earnest_pct", "1") or "1") / 100,
                "default_option_fee": int(float(request.form.get("option_fee", "250") or "250")),
            })
            saved = True

    existing = get_agent_profile(phone) if phone else {}

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
    --bg: #0f172a;
    --bg-elevated: #1e293b;
    --bg-card: rgba(255,255,255,0.03);
    --border: rgba(255,255,255,0.06);
    --border-hover: rgba(16,185,129,0.3);
    --text: #f8fafc;
    --text-muted: #94a3b8;
    --text-dim: #64748b;
    --accent: #10b981;
    --accent-light: #34d399;
    --radius: 1.25rem;
    --radius-sm: 0.75rem;
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
    background:rgba(15,23,42,0.9);backdrop-filter:blur(16px);
    -webkit-backdrop-filter:blur(16px);
    border-bottom:1px solid var(--border);z-index:100;
  }}
  .nav-left {{display:flex;align-items:center;gap:0.6rem;font-weight:700;font-size:1.1rem;letter-spacing:-0.02em;}}
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
  .nav-cta:hover {{transform:scale(1.05);box-shadow:0 0 24px rgba(16,185,129,0.4);}}

  .container {{max-width:520px;margin:0 auto;padding:3rem 1.5rem 4rem;}}
  .page-header {{margin-bottom:2rem;}}
  .page-header h1 {{font-size:1.75rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.25rem;}}
  .page-header p {{color:var(--text-muted);font-size:0.9rem;}}

  .form-card {{
    background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
    padding:2rem;
  }}
  .field-label {{
    font-size:0.7rem;font-weight:700;color:var(--text-dim);
    text-transform:uppercase;letter-spacing:0.07em;margin-bottom:0.5rem;display:block;
    margin-top:1.25rem;
  }}
  .field-label:first-child {{margin-top:0;}}
  .form-card input {{
    width:100%;background:rgba(0,0,0,0.35);border:1px solid rgba(255,255,255,0.1);
    border-radius:var(--radius-sm);padding:0.75rem 1rem;color:var(--text);
    font-size:0.9rem;font-family:inherit;outline:none;transition:var(--transition);
  }}
  .form-card input:focus {{border-color:var(--accent);box-shadow:0 0 0 3px rgba(16,185,129,0.15);}}
  .form-card input::placeholder {{color:#475569;}}
  .row {{display:flex;gap:0.75rem;}}
  .row > div {{flex:1;}}
  .form-card button {{
    width:100%;margin-top:1.5rem;
    background:linear-gradient(135deg,var(--accent),#059669);color:#fff;border:none;
    border-radius:var(--radius-sm);padding:0.85rem;font-weight:600;font-size:0.95rem;
    font-family:inherit;cursor:pointer;transition:var(--transition);
  }}
  .form-card button:hover {{transform:translateY(-2px);box-shadow:0 8px 24px rgba(16,185,129,0.35);}}
  .success {{
    margin-top:1rem;padding:0.85rem 1rem;
    background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.2);
    border-radius:var(--radius-sm);font-size:0.85rem;color:var(--accent-light);text-align:center;
  }}
  .error {{
    margin-top:1rem;padding:0.85rem 1rem;
    background:rgba(248,113,113,0.08);border:1px solid rgba(248,113,113,0.2);
    border-radius:var(--radius-sm);font-size:0.85rem;color:#f87171;text-align:center;
  }}
  .foot {{text-align:center;margin-top:1.5rem;font-size:0.8rem;color:var(--text-dim);}}
  .foot a {{color:var(--accent-light);}}
  .foot a:hover {{text-decoration:underline;}}

  @media(max-width:600px){{
    .container {{padding:2rem 1rem 3rem;}}
    .form-card {{padding:1.5rem 1.25rem;}}
    .nav-links {{display:none;}}
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
  <div class="nav-links">
    <a href="/">Home</a>
    <a href="/demo">Demo</a>
    <a href="/pricing">Pricing</a>
  </div>
  <a href="/signup" class="nav-cta">Start Free Trial</a>
</nav>

<div class="container">
  <div class="page-header">
    <h1>Agent Profile</h1>
    <p>Your info auto-fills the cover page on every offer you generate.</p>
  </div>

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
:root{{--bg:#0f172a;--bg-card:rgba(255,255,255,0.03);--border:rgba(255,255,255,0.06);
--text:#f8fafc;--text-muted:#94a3b8;--text-dim:#64748b;--accent:#10b981;--accent-light:#34d399;
--radius:1.25rem;--radius-sm:0.75rem;}}
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;
-webkit-font-smoothing:antialiased;}}
.top-bar{{background:rgba(16,185,129,0.1);border-bottom:1px solid rgba(16,185,129,0.2);
padding:0.6rem 1.5rem;text-align:center;font-size:0.8rem;color:var(--accent-light);font-weight:600;}}
.container{{max-width:600px;margin:0 auto;padding:1.5rem 1rem;}}
.address-card{{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
padding:1.5rem;text-align:center;margin-bottom:1rem;}}
.address-card h1{{font-size:1.25rem;font-weight:700;margin-bottom:0.25rem;}}
.address-card .meta{{color:var(--text-dim);font-size:0.8rem;}}
.stats{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:0.5rem;margin-bottom:1rem;}}
.stat{{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-sm);
padding:0.85rem 0.5rem;text-align:center;}}
.stat-label{{font-size:0.65rem;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;
color:var(--text-dim);margin-bottom:0.2rem;}}
.stat-value{{font-size:1rem;font-weight:700;}}
.stat-value.accent{{color:var(--accent-light);}}
.actions{{display:flex;flex-direction:column;gap:0.6rem;margin-bottom:1.25rem;}}
.btn{{display:flex;align-items:center;justify-content:center;gap:0.5rem;padding:0.9rem 1rem;
border-radius:var(--radius-sm);font-family:inherit;font-size:0.9rem;font-weight:600;
text-decoration:none;border:none;cursor:pointer;transition:all 0.2s;}}
.btn-primary{{background:linear-gradient(135deg,var(--accent),#059669);color:#fff;}}
.btn-primary:hover{{transform:translateY(-1px);box-shadow:0 6px 20px rgba(16,185,129,0.3);}}
.btn-secondary{{background:var(--bg-card);color:var(--text);border:1px solid var(--border);}}
.btn-secondary:hover{{border-color:var(--accent);}}
.btn-outline{{background:transparent;color:var(--text-muted);border:1px solid var(--border);}}
.btn-outline:hover{{border-color:var(--accent);color:var(--accent-light);}}
.pdf-frame{{width:100%;height:70vh;border:1px solid var(--border);border-radius:var(--radius-sm);
background:#1e293b;}}
.email-form{{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
padding:1.25rem;margin-bottom:1rem;display:none;}}
.email-form.show{{display:block;}}
.email-form label{{font-size:0.8rem;font-weight:600;color:var(--text-dim);display:block;margin-bottom:0.4rem;}}
.email-form input{{width:100%;padding:0.7rem;background:rgba(255,255,255,0.04);border:1px solid var(--border);
border-radius:var(--radius-sm);color:var(--text);font-family:inherit;font-size:0.9rem;outline:none;
margin-bottom:0.75rem;}}
.email-form input:focus{{border-color:var(--accent);}}
.email-status{{font-size:0.85rem;padding:0.5rem;border-radius:var(--radius-sm);margin-top:0.5rem;display:none;}}
.email-status.success{{display:block;background:rgba(16,185,129,0.1);color:var(--accent-light);}}
.email-status.error{{display:block;background:rgba(239,68,68,0.1);color:#fca5a5;}}
.sent-banner{{background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.25);color:var(--accent-light);
border-radius:var(--radius-sm);padding:0.75rem 1rem;text-align:center;font-size:0.85rem;margin-bottom:1rem;}}
.sent-banner strong{{color:var(--text);}}
.disclaimer{{font-size:0.75rem;color:var(--text-dim);text-align:center;padding:1rem;
border-top:1px solid var(--border);margin-top:1rem;}}
.btn:disabled{{opacity:0.45;cursor:not-allowed;}}
.btn:disabled:hover{{transform:none;box-shadow:none;}}
.qa-blocking, .qa-warnings{{border-radius:var(--radius-sm);padding:0.85rem 1rem;margin-bottom:0.85rem;font-size:0.85rem;}}
.qa-blocking{{background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.25);color:#fca5a5;}}
.qa-warnings{{background:rgba(251,191,36,0.08);border:1px solid rgba(251,191,36,0.25);color:#fbbf24;}}
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

<div class="actions">
<button class="btn btn-primary" id="email-toggle"{' disabled' if validation['blocking'] else ''}>{'Resend to Listing Agent' if email_sent_at else 'Email to Listing Agent'}</button>
<a href="{pdf_url}" class="btn btn-secondary" target="_blank">Open PDF</a>
<a href="{pdf_url}" class="btn btn-outline" download="{filename}">Download PDF</a>
</div>

<div class="email-form" id="email-form">
<label>Listing agent's email</label>
<input type="email" id="email-to" placeholder="agent@example.com" value="{email_sent_to}">
<button class="btn btn-primary" id="send-email-btn" style="width:100%;">Send Offer PDF</button>
<div class="email-status" id="email-status"></div>
</div>

<iframe src="{pdf_url}" class="pdf-frame" title="Offer PDF"></iframe>

<div class="disclaimer">
This is a draft generated by TxtAnOffer. Agent must review all fields before signing or presenting.
Not affiliated with TREC. &middot; <a href="/" style="color:var(--accent-light);">txtanoffer.com</a>
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
}})();
</script>
</body>
</html>"""


THREAD_EXPIRED_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Offer Thread - TxtAnOffer</title>
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet"></noscript>
<style>
body{font-family:'Inter',sans-serif;background:#0f172a;color:#f8fafc;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px;}
.box{background:rgba(255,255,255,0.03);border-radius:1.25rem;padding:2.5rem;max-width:400px;text-align:center;
border:1px solid rgba(255,255,255,0.06);}
h2{margin:0 0 0.75rem;font-size:1.35rem;font-weight:700;}
p{color:#94a3b8;font-size:0.9rem;line-height:1.6;}
a{color:#34d399;text-decoration:none;}
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
:root{{--bg:#0f172a;--bg-card:rgba(255,255,255,0.03);--border:rgba(255,255,255,0.06);
--text:#f8fafc;--text-muted:#94a3b8;--text-dim:#64748b;--accent:#10b981;--accent-light:#34d399;
--radius:1.25rem;--radius-sm:0.75rem;}}
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;
-webkit-font-smoothing:antialiased;}}
.top-bar{{background:rgba(16,185,129,0.1);border-bottom:1px solid rgba(16,185,129,0.2);
padding:0.6rem 1.5rem;text-align:center;font-size:0.8rem;color:var(--accent-light);font-weight:600;}}
.container{{max-width:600px;margin:0 auto;padding:1.5rem 1rem;}}
.address-card{{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
padding:1.5rem;text-align:center;margin-bottom:1rem;}}
.address-card h1{{font-size:1.25rem;font-weight:700;margin-bottom:0.25rem;}}
.address-card .meta{{color:var(--text-dim);font-size:0.8rem;}}
.stats{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:0.5rem;margin-bottom:1rem;}}
.stat{{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-sm);
padding:0.85rem 0.5rem;text-align:center;}}
.stat-label{{font-size:0.65rem;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;
color:var(--text-dim);margin-bottom:0.2rem;}}
.stat-value{{font-size:1rem;font-weight:700;}}
.stat-value.accent{{color:var(--accent-light);}}
.actions{{display:flex;flex-direction:column;gap:0.6rem;margin-bottom:1.25rem;}}
.btn{{display:flex;align-items:center;justify-content:center;gap:0.5rem;padding:0.9rem 1rem;
border-radius:var(--radius-sm);font-family:inherit;font-size:0.9rem;font-weight:600;
text-decoration:none;border:none;cursor:pointer;transition:all 0.2s;}}
.btn-primary{{background:linear-gradient(135deg,var(--accent),#059669);color:#fff;}}
.btn-primary:hover{{transform:translateY(-1px);box-shadow:0 6px 20px rgba(16,185,129,0.3);}}
.btn-outline{{background:transparent;color:var(--text-muted);border:1px solid var(--border);}}
.btn-outline:hover{{border-color:var(--accent);color:var(--accent-light);}}
.pdf-frame{{width:100%;height:70vh;border:1px solid var(--border);border-radius:var(--radius-sm);
background:#1e293b;}}
.disclaimer{{font-size:0.75rem;color:var(--text-dim);text-align:center;padding:1rem;
border-top:1px solid var(--border);margin-top:1rem;}}
.notbinding{{font-size:0.78rem;color:var(--text-muted);background:var(--bg-card);border:1px solid var(--border);
border-radius:var(--radius-sm);padding:0.75rem 1rem;margin-bottom:1.25rem;line-height:1.5;}}
.status-panel{{background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.25);color:var(--accent-light);
border-radius:var(--radius-sm);padding:0.9rem 1rem;text-align:center;font-size:0.9rem;margin-bottom:1.25rem;}}
.qa-blocking, .qa-warnings{{border-radius:var(--radius-sm);padding:0.85rem 1rem;margin-bottom:0.85rem;font-size:0.85rem;}}
.qa-blocking{{background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.25);color:#fca5a5;}}
.qa-warnings{{background:rgba(251,191,36,0.08);border:1px solid rgba(251,191,36,0.25);color:#fbbf24;}}
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

<div class="disclaimer">
This is a draft generated by TxtAnOffer. Not affiliated with TREC. &middot; <a href="/" style="color:var(--accent-light);">txtanoffer.com</a>
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
body{font-family:'Inter',sans-serif;background:#0f172a;color:#f8fafc;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px;}
.box{background:rgba(255,255,255,0.03);border-radius:1.25rem;padding:2.5rem;max-width:400px;text-align:center;
border:1px solid rgba(255,255,255,0.06);}
h2{margin:0 0 0.75rem;font-size:1.35rem;font-weight:700;}
p{color:#94a3b8;font-size:0.9rem;line-height:1.6;}
a{color:#34d399;text-decoration:none;}
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

    initials = "".join(part[0] for part in agent.get("name", "").split()[:2]).upper() if agent.get("name") else "?"

    if is_admin_phone(phone):
        sub_status = "Admin (Unlimited)"
        sub_badge_color = "rgba(157,148,255,0.15)"
        sub_badge_text = "#9d94ff"
    elif user["is_subscribed"]:
        sub_status = "Active"
        sub_badge_color = "rgba(16,185,129,0.15)"
        sub_badge_text = "var(--accent-light)"
    else:
        sub_status = f"Free ({user['offer_count']}/{FREE_OFFER_LIMIT} used)"
        sub_badge_color = "rgba(251,191,36,0.15)"
        sub_badge_text = "#fbbf24"

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
    --bg: #0f172a;
    --bg-elevated: #1e293b;
    --bg-card: rgba(255,255,255,0.03);
    --border: rgba(255,255,255,0.06);
    --border-hover: rgba(16,185,129,0.3);
    --text: #f8fafc;
    --text-muted: #94a3b8;
    --text-dim: #64748b;
    --accent: #10b981;
    --accent-light: #34d399;
    --radius: 1.25rem;
    --radius-sm: 0.75rem;
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
    background:rgba(15,23,42,0.9);backdrop-filter:blur(16px);
    -webkit-backdrop-filter:blur(16px);
    border-bottom:1px solid var(--border);z-index:100;
  }}
  .nav-left {{display:flex;align-items:center;gap:0.6rem;font-weight:700;font-size:1.1rem;letter-spacing:-0.02em;}}
  .nav-logo {{width:34px;height:34px;border-radius:22%;overflow:hidden;}}
  .nav-logo img {{width:100%;height:100%;object-fit:contain;}}
  .nav-links {{display:flex;gap:2rem;font-size:0.875rem;font-weight:500;color:var(--text-muted);}}
  .nav-links a {{transition:var(--transition);}}
  .nav-links a:hover {{color:var(--text);}}

  .container {{max-width:1000px;margin:0 auto;padding:2.5rem 2rem 4rem;}}
  .greeting {{font-size:1.75rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.5rem;}}
  .sub-badge {{
    display:inline-block;background:{sub_badge_color};color:{sub_badge_text};
    padding:0.3rem 0.85rem;border-radius:9999px;font-size:0.75rem;font-weight:700;
    letter-spacing:0.02em;
  }}

  .stats {{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1rem;margin:2rem 0;}}
  .stat {{
    background:linear-gradient(180deg, var(--bg-elevated), #131c2e);
    border:1px solid var(--border);border-radius:var(--radius);
    padding:1.5rem;transition:var(--transition);
    box-shadow:0 4px 20px rgba(0,0,0,0.25);
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
    background:linear-gradient(180deg, var(--bg-elevated), #131c2e);
    border:1px solid var(--border);border-radius:var(--radius);
    padding:1.5rem 1.75rem;margin-top:0.5rem;
    box-shadow:0 4px 20px rgba(0,0,0,0.25);
  }}
  .profile-card-head {{display:flex;align-items:center;justify-content:space-between;margin-bottom:1rem;}}
  .profile-card-head h2 {{margin:0;}}
  .profile-edit-link {{
    font-size:0.8rem;font-weight:600;color:var(--accent-light);
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
  .id-name {{font-weight:700;font-size:1.05rem;}}
  .id-meta {{font-size:0.8rem;color:var(--text-muted);margin-top:0.2rem;}}

  .profile-grid {{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:1.1rem;}}
  .profile-field-label {{font-size:0.7rem;font-weight:600;color:var(--text-dim);
    text-transform:uppercase;letter-spacing:0.06em;margin-bottom:0.25rem;}}
  .profile-field-val {{font-size:0.9rem;color:var(--text);}}
  .profile-field-val.unset {{color:var(--text-dim);font-style:italic;}}
  .profile-empty {{color:var(--text-muted);font-size:0.9rem;line-height:1.6;}}
  .profile-empty a {{color:var(--accent-light);font-weight:600;}}

  h2 {{font-size:1.1rem;font-weight:700;margin:2.5rem 0 1rem;}}

  .offer-feed {{display:flex;flex-direction:column;gap:0.9rem;}}
  .offer-card {{
    display:flex;border-radius:var(--radius);overflow:hidden;
    background:linear-gradient(180deg, var(--bg-elevated), #131c2e);
    border:1px solid var(--border);
    box-shadow:0 4px 20px rgba(0,0,0,0.25);
    transition:var(--transition);
  }}
  .offer-card:hover {{border-color:var(--border-hover);}}
  .offer-card:active {{transform:scale(0.99);}}
  .offer-card-bar {{width:4px;flex-shrink:0;background:linear-gradient(180deg, var(--accent), var(--accent-light));}}
  .offer-card-bar.status-draft {{background:linear-gradient(180deg, #10b981, #34d399);}}
  .offer-card-bar.status-sent {{background:linear-gradient(180deg, #f59e0b, #fbbf24);}}
  .offer-card-bar.status-expired {{background:linear-gradient(180deg, #6b7280, #9ca3af);}}
  .offer-card-bar.status-accepted {{background:linear-gradient(180deg, #3b82f6, #60a5fa);}}
  .offer-card-bar.status-declined {{background:linear-gradient(180deg, #f43f5e, #fb7185);}}
  .offer-card-body {{padding:1.25rem 1.5rem;flex:1;min-width:0;}}
  .offer-top {{display:flex;align-items:baseline;justify-content:space-between;gap:0.75rem;margin-bottom:0.9rem;}}
  .offer-addr-wrap {{display:flex;align-items:center;gap:0.6rem;min-width:0;}}
  .offer-addr {{font-weight:700;font-size:0.98rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
  .offer-date {{font-size:0.75rem;color:var(--text-dim);flex-shrink:0;}}
  .status-badge {{
    font-size:0.65rem;font-weight:700;text-transform:uppercase;letter-spacing:0.04em;
    padding:0.2rem 0.55rem;border-radius:9999px;flex-shrink:0;
  }}
  .status-badge.status-draft {{background:rgba(16,185,129,0.12);color:#10b981;}}
  .status-badge.status-sent {{background:rgba(245,158,11,0.12);color:#f59e0b;}}
  .status-badge.status-expired {{background:rgba(107,114,128,0.12);color:#9ca3af;}}
  .status-badge.status-accepted {{background:rgba(59,130,246,0.12);color:#3b82f6;}}
  .status-badge.status-declined {{background:rgba(244,63,94,0.12);color:#f43f5e;}}

  .pills {{display:flex;gap:0.6rem;margin-bottom:1rem;flex-wrap:wrap;}}
  .pill {{
    background:rgba(255,255,255,0.04);border:1px solid var(--border);border-radius:var(--radius-sm);
    padding:0.5rem 0.85rem;display:flex;flex-direction:column;gap:0.1rem;min-width:72px;
  }}
  .pill-val {{font-size:0.9rem;font-weight:700;color:var(--text);}}
  .pill-label {{font-size:0.65rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.05em;}}

  .amend-list {{margin:0 0 1rem;padding:0.75rem 0.9rem;background:rgba(255,255,255,0.02);
    border:1px solid var(--border);border-radius:var(--radius-sm);}}
  .amend-row {{display:flex;align-items:center;justify-content:space-between;gap:0.75rem;
    font-size:0.78rem;color:var(--text-dim);padding:0.25rem 0;}}
  .amend-desc {{color:var(--text-muted);}}
  .amend-pdf {{color:var(--accent-light);font-weight:600;flex-shrink:0;}}
  .amend-pdf:hover {{text-decoration:underline;}}

  .btn-primary {{
    display:inline-block;background:var(--accent);color:#06281d;font-weight:700;
    padding:0.6rem 1.1rem;border-radius:var(--radius-sm);font-size:0.85rem;
    box-shadow:0 2px 10px rgba(16,185,129,0.3);transition:var(--transition);
  }}
  .btn-primary:hover {{background:var(--accent-light);}}
  .btn-primary:active {{transform:scale(0.97);}}

  .empty-state {{
    text-align:center;color:var(--text-dim);padding:3rem 1.5rem;
    background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
    line-height:1.7;
  }}

  .bottom-nav {{
    position:sticky;bottom:0;display:flex;justify-content:space-around;
    background:rgba(15,23,42,0.92);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
    border-top:1px solid var(--border);padding:0.7rem 0 calc(0.7rem + env(safe-area-inset-bottom));
    margin-top:2.5rem;
  }}
  .nav-item {{
    display:flex;flex-direction:column;align-items:center;gap:0.2rem;
    font-size:0.65rem;font-weight:600;color:var(--text-dim);transition:var(--transition);
  }}
  .nav-item span.icon {{font-size:1.2rem;}}
  .nav-item:hover, .nav-item.active {{color:var(--accent-light);}}

  @media(max-width:600px){{
    .container {{padding:1.5rem 1rem 1rem;}}
    .stats {{grid-template-columns:1fr 1fr 1fr;}}
    .greeting {{font-size:1.35rem;}}
    .nav-links {{display:none;}}
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
  <div class="nav-links">
    <a href="{profile_url}">Edit Profile</a>
    <a href="/pricing">Pricing</a>
  </div>
</nav>

<div class="container">
  <div class="greeting">Welcome back{', ' + agent.get('name').split()[0] if agent.get('name') else ''}</div>
  <span class="sub-badge">{sub_status}</span>

  <div class="stats">
    <div class="stat"><div class="stat-val">{user['offer_count']}</div><div class="stat-label">Total offers</div></div>
    <div class="stat"><div class="stat-val">{time_saved}</div><div class="stat-label">Time saved</div></div>
    <div class="stat"><div class="stat-val">{avg_close}</div><div class="stat-label">Avg close</div></div>
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
