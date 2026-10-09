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
from urllib.parse import quote as _urlquote, urlparse as _urlparse
import stripe
import difflib
import requests as http_requests
from twilio.rest import Client as TwilioClient

from parser import parse_offer_sms, parse_amendment_sms, parse_correction_sms
from pdf_filler import fill_offer_pdf, OUTPUT_DIR
from pdf_validator import validate_offer_pdf
from amendment import fill_amendment_pdf
from agent_profiles import get_agent_profile, save_agent_profile, find_by_email, get_emails_for_phones
from subscriptions import can_generate_offer, increment_offer_count, activate_subscription, deactivate_subscription, get_user, create_user, FREE_OFFER_LIMIT, is_admin_phone, has_professional_access
from analytics import track_event, get_conversion_metrics, get_revenue_metrics, get_recent_sms, get_recent_sms_failures, get_last_blocked_state, get_waitlist_signups, get_signups_by_source, get_signup_details, get_landing_visits_by_source, get_tc_check_summary, get_recent_tc_check_email_senders, get_tc_check_count_for_sender, get_tc_check_repeat_senders, get_tc_check_bulk_summary, get_tc_check_attempts_by_source, get_tc_check_attempts_by_page, get_brokerage_alert_delivery, get_daily_funnel, get_top_referrers, set_internal_visitor, get_visitor_roles, get_engagement_by_device, get_recent_email_send_failures, get_recent_visitors, get_archive_early_access
from integrations import send_offer_email, fire_webhook, save_webhook, get_webhook, delete_webhook, send_to_docusign, send_plain_email, send_html_email
from offers_db import record_offer, get_offers_for_phone, get_offer_by_filename, record_amendment, get_amendments_for_phone, record_thread_response, record_email_sent, record_docusign_sent
from brokerages import extract_brokerage_prefix, link_user_to_brokerage, get_brokerage, get_brokerage_by_code, create_brokerage, list_brokerages, list_brokerage_agents, list_brokerage_records
from sponsors import create_sponsor, list_sponsors, set_sponsor_active
from sms_utils import parse_incoming_sms
from cleanup import run_cleanup_if_due
import archive as archive_store
import crm
import broker_auth
from reminders import run_reminders_if_due
from deadlines import earnest_money_deadline, option_end_date, build_day_one_summary
import transaction_tasks
from drafts import save_draft, get_draft, clear_draft
from tc_audit import check_tc_file, compare_contracts
from tc_report_pdf import generate_tc_report_pdf
from rate_limit import check_and_increment
from tc_gate import get_client as get_tc_client, record_use as record_tc_use, save_email as save_tc_email
from tc_nudge import run_followup_if_due as run_tc_followup_if_due
from tc_check_email import (
    extract_sender_email, extract_pdf_attachments, junk_sender_reason,
    format_reply_body, format_reply_html, subject_line,
    format_no_pdf_reply, format_no_pdf_html,
    format_unreadable_reply, format_unreadable_html,
)
from tc_bulk import (
    extract_pdfs_from_zip, create_batch, get_batch, process_batch,
    BulkUploadError, MAX_BULK_FILES, FREE_BULK_LIMIT,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from html import escape
import shutil
import tempfile
import threading
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
# Raised from 15MB to cover /tc-check/bulk's zip upload (up to 200 files) --
# tc_bulk.py enforces its own tighter per-file/per-batch byte caps on top of
# this, and both bulk routes are rate-limited separately (see tc_bulk_ip/
# tc_bulk_email below), so this ceiling only bounds worst-case body size.
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

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

# Public-facing TC Check stats (homepage stat strip, upload-gate social
# proof, /tc-hub "Common TC Mistakes"). Off as of 2026-10-02: the 30-day
# window was almost entirely the owner's own test uploads (mostly blank
# templates), so "N files scanned, 100% missing buyer name" was true of
# the event log but false as a claim about real Texas contracts. Turn
# back on only once real third-party files dominate the sample -- check
# /analytics' Daily Funnel and Recent Email Senders first.
PUBLIC_TC_STATS_ENABLED = False

# Free full reports per browser before the email gate applies (see
# tc_check()). 3, not 1: the homepage widget hands its file to /tc-check
# for the full checklist, which re-runs the check, so a 1-check allowance
# would gate the visitor on that very click-through.
TC_FREE_FULL_REPORTS = 3

# TC Check email-forward intake: SendGrid Inbound Parse has no request
# signing, so this shared secret lives in the webhook path itself
# (/v1/tc/check/email/<token>) -- see tc_check_email_inbound()'s docstring.
TC_CHECK_EMAIL_TOKEN = os.environ.get("TC_CHECK_EMAIL_TOKEN", "")

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


import re
# "What brings you here?" self-ID card (added 2026-10-06, removed
# 2026-10-09: shown to 14 real browsers, 0 answered). The card is gone;
# these keys stay so the /analytics panel keeps showing its results.
_VISITOR_ROLES = ("tc", "agent", "broker", "investor", "browsing")

_BOT_UA_RE = re.compile(
    r"bot|crawl|spider|slurp|preview|facebookexternalhit|embedly|curl|wget|"
    r"python-requests|httpx|aiohttp|go-http-client|headless|lighthouse|uptime|monitor|"
    # Google's URL Inspection / other fetchers don't say "bot" (added 2026-10-07
    # after "Request indexing" showed up as 16 phantom visitors).
    r"inspectiontool|googleother|google-read-aloud|apis-google|feedfetcher|"
    # Crawler posing as a 2019 iPhone (iOS 13.2.3 / Safari 13.0.3), hit the
    # guides every ~10 min with no JS on 2026-10-07.
    r"iphone os 13_2_3 like mac os x.*version/13\.0\.3",
    re.I,
)


_MOBILE_UA_RE = re.compile(r"Mobi|Android|iPhone|iPad|iPod", re.I)
_ENGAGEMENT_TYPES = ("js_ok", "hero_cta", "hero_sample", "dropzone_seen", "stay_10s", "stay_60s",
                     "role_shown", "role_dismissed", "brokers_audit_cta", "brokers_email_cta", "brokers_plan_cta", "records_brokers_cta", "guide_check_cta", "guide_brokers_cta", "path_self_cta", "path_walkthrough_cta", "path_brokerage_cta", "guide_card_cta", "support_email_cta", "pricing_audit_cta") + tuple("role_" + r for r in _VISITOR_ROLES)

# Tiny, cookie-less-of-its-own beacon script appended to every page that
# track_page_view() logs. Elements opt in with data-evt="<type>" for clicks;
# the drop zone (homepage or /tc-check) reports once when half-visible, and
# a visible-time counter reports a 10s / 60s stay, so /analytics can tell "left in 5
# seconds" apart from "read it but never scrolled to the drop box".
_ENGAGEMENT_JS = """<script>
(function(){
  var page = document.body.getAttribute('data-page') || '';
  var sent = {};
  function evt(t){
    if(sent[t]) return; sent[t] = true;
    try{ var f = new FormData(); f.append('type', t); f.append('page', page);
      if(!(navigator.sendBeacon && navigator.sendBeacon('/v1/evt', f))) fetch('/v1/evt', {method:'POST', body:f, keepalive:true}); }catch(e){}
  }
  // Fires on load: most scanners/link-preview fetchers never run JS, so
  // page_view minus js_ok approximates "not a real browser".
  evt('js_ok');
  document.addEventListener('click', function(e){
    var el = e.target.closest && e.target.closest('[data-evt]');
    if(el) evt(el.getAttribute('data-evt'));
  }, true);
  var dz = document.getElementById('homeDropZone') || document.getElementById('dropZone');
  if(dz && 'IntersectionObserver' in window){
    new IntersectionObserver(function(es, o){ es.forEach(function(x){ if(x.isIntersecting){ evt('dropzone_seen'); o.disconnect(); } }); }, {threshold:0.5}).observe(dz);
  }
  // Visible time, not wall-clock: a link opened in a background tab only
  // starts counting once the visitor actually looks at it.
  var visibleMs = 0, last = Date.now();
  var tick = setInterval(function(){
    var now = Date.now();
    if(!document.hidden) visibleMs += now - last;
    last = now;
    if(visibleMs >= 10000) evt('stay_10s');
    if(visibleMs >= 60000){ evt('stay_60s'); clearInterval(tick); }
  }, 1000);
})();
</script>"""


def _is_mobile_request() -> bool:
    return bool(_MOBILE_UA_RE.search(request.headers.get("User-Agent", "")))


def track_page_view(resp, page: str):
    """Logs a 'page_view' for every non-bot load of a landing page, tagged
    or not. Added 2026-10-02: 'landing_visit' only fires on first-touch
    ?src= links, so organic/typed/shared-link traffic was invisible and
    there was no real visits -> widget-attempts funnel. Link-preview
    fetchers (LinkedInBot, Slackbot, iMessage previews...) are skipped by
    User-Agent so pasting a link somewhere doesn't count as a visit.
    A random 'ta_vid' cookie lets get_daily_funnel() count unique
    visitors, not just page loads."""
    ua = request.headers.get("User-Agent", "")
    if not ua or _BOT_UA_RE.search(ua):
        return
    html = resp.get_data(as_text=True)
    if "</body>" in html:
        resp.set_data(html.replace("<body>", '<body data-page="' + page + '">', 1).replace("</body>", _ENGAGEMENT_JS + "</body>", 1))
    visitor = request.cookies.get("ta_vid", "")
    if not re.fullmatch(r"[0-9a-f]{32}", visitor):
        visitor = uuid.uuid4().hex
        resp.set_cookie("ta_vid", visitor, max_age=365 * 24 * 3600, httponly=True, samesite="Lax")
    referrer = (_urlparse(request.referrer or "").hostname or "").lower()
    if referrer.startswith("www."):
        referrer = referrer[4:]
    if referrer == "txtanoffer.com":
        referrer = "(internal)"
    src = re.sub(r"[^a-zA-Z0-9_-]", "", request.args.get("src", ""))[:60]
    track_event("page_view", None, {
        "page": page,
        "visitor": visitor,
        "referrer": referrer,
        "source": src or request.cookies.get("ta_src") or "direct",
        "device": "mobile" if _is_mobile_request() else "desktop",
        # Raw User-Agent (truncated), so /analytics' Recent Visitors table can
        # tell a link scanner posing as Chrome from a person. Added 2026-10-07.
        "ua": ua[:160],
    })


@app.route("/v1/evt", methods=["POST"])
def engagement_event():
    """Beacon target for _ENGAGEMENT_JS. Fixed whitelist of event types and
    pages, tied to the same ta_vid as page_view so the Daily Funnel can
    count unique visitors per step. Always 204 -- nothing for a caller to
    act on, and nothing worth erroring about."""
    ua = request.headers.get("User-Agent", "")
    etype = request.form.get("type", "")
    page = request.form.get("page", "")
    visitor = request.cookies.get("ta_vid", "")
    if (ua and not _BOT_UA_RE.search(ua) and etype in _ENGAGEMENT_TYPES
            and page in ("homepage", "tc_check_page", "brokers_page", "guide_page") and re.fullmatch(r"[0-9a-f]{32}", visitor)):
        track_event("page_engagement", None, {
            "type": etype, "page": page, "visitor": visitor,
            "device": "mobile" if _is_mobile_request() else "desktop",
        })
    return "", 204


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


def sign_transaction_url(filename, base_url=""):
    # Same signature scheme as /review and /offers (verify_pdf_signature is
    # generic on filename+expires+sig) -- this is a TC-only link, minted
    # here and handed out via the dashboard, never the public /thread page.
    expires, sig = sign_pdf_view_params(filename)
    return f"{base_url}/transaction/{filename}?expires={expires}&sig={sig}"


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
  <title>TxtAnOffer — Catch What Title Kicks Back</title>
  <meta name="description" content="Built for Texas managing brokers and transaction coordinators: drop a filled TREC 20-19 (and its 40-11 addendum) and see what's missing or inconsistent before title kicks it back. TxtAnOffer also drafts new offers by text message in 10 seconds.">
  <meta property="og:title" content="TxtAnOffer — Catch What Title Kicks Back">
  <meta property="og:description" content="Drop a filled TREC 20-19 and see what's missing or inconsistent before title kicks it back &mdash; blank dates, missing initials, mismatched checkboxes.">
  <meta property="og:url" content="https://txtanoffer.com/">
  <meta property="og:type" content="website">
  <meta name="twitter:card" content="summary">
  <meta name="twitter:title" content="TxtAnOffer — Catch What Title Kicks Back">
  <meta name="twitter:description" content="Drop a filled TREC 20-19 and see what's missing or inconsistent before title kicks it back &mdash; blank dates, missing initials, mismatched checkboxes.">
  <link rel="icon" href="/static/favicon.ico" type="image/x-icon">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Source+Serif+4:opsz,wght@8..60,500;8..60,600&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Source+Serif+4:opsz,wght@8..60,500;8..60,600&display=swap" rel="stylesheet"></noscript>
  <style>
    :root {
      --bg: #F5F5F7;
      --card-dark: #0f1f2f;
      --card-dark-2: #152a3a;
      --card-dark-3: #112333;
      --bg-card: #fff;
      --border: rgba(15,31,47,0.08);
      --border-hover: rgba(11,93,82,0.35);
      --text: #0f1f2f;
      --text-muted: #5a6b7a;
      --text-dim: #8a9aa9;
      --accent: #0b5d52;
      --accent-light: #16806e;
      --accent-tint: #E7F3F1;
      --accent-glow: rgba(11,93,82,0.18);
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
    /* Ambient hero orb -- attached to body (not .hero) so the glow bleeds to the
       real page edges instead of stopping at .main's 840px content width. */
    body::before {
      content: '';
      position: absolute;
      width: 400px; height: 400px;
      background: linear-gradient(135deg, rgba(15,31,47,0.10), rgba(15,31,47,0.05));
      border-radius: 50%;
      filter: blur(80px);
      top: -120px; right: 0;
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
      background: var(--accent);
      color: #fff;
      padding: 0.55rem 1.35rem;
      border-radius: 9999px;
      font-size: 0.875rem;
      font-weight: 600;
      border: 1px solid var(--accent);
      cursor: pointer;
      transition: var(--transition);
      display: inline-block;
      text-decoration: none;
    }
    .nav-cta:hover { background: var(--accent-light); border-color: var(--accent-light); }
    .nav-toggle { display: none; flex-direction: column; justify-content: center; gap: 5px; width: 34px; height: 34px; background: none; border: none; cursor: pointer; padding: 0; }
    .nav-toggle span { display: block; width: 100%; height: 2px; background: var(--text); border-radius: 2px; }

    /* Sun Life-style hero band: deep teal panel + full-bleed photo */
    .sl-hero { display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 7fr); min-height: 470px; position: relative; z-index: 1; }
    .sl-hero-panel { background: #0a3f3a; color: #fff; padding: 3.5rem 3rem 3.5rem max(2rem, calc((100vw - 1100px) / 2)); display: flex; flex-direction: column; justify-content: center; }
    .sl-hero-panel h1 { font-family: 'Source Serif 4', Georgia, 'Times New Roman', serif; font-weight: 600; font-size: 2.45rem; line-height: 1.14; letter-spacing: -0.01em; color: #fff; margin: 0; max-width: 30rem; }
    .sl-hero-panel p { margin: 1.15rem 0 0; color: rgba(255,255,255,0.86); font-size: 1.02rem; line-height: 1.6; max-width: 27rem; }
    .sl-cta { display: inline-block; margin-top: 1.75rem; background: #f5c242; color: #0f1f2f; font-weight: 700; font-size: 1rem; padding: 0.95rem 1.6rem; border-radius: 4px; text-decoration: none; width: fit-content; transition: background 0.18s ease; }
    .sl-cta:hover { background: #ffd25e; }
    .sl-hero-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 0.9rem 1.25rem; margin-top: 1.75rem; }
    .sl-hero-actions .sl-cta { margin-top: 0; }
    .sl-cta-secondary { background: none; border: 0; padding: 0.5rem 0; font: inherit; font-size: 0.98rem; font-weight: 600; color: #fff; text-decoration: underline; text-underline-offset: 4px; text-decoration-color: rgba(255,255,255,0.45); cursor: pointer; }
    .sl-cta-secondary:hover { text-decoration-color: #fff; }
    .sl-cta-secondary:disabled { opacity: 0.75; cursor: default; }
    .sl-hero-phone { display: none; margin-top: 1rem; font-size: 0.86rem; line-height: 1.5; color: rgba(255,255,255,0.85); }
    .sl-hero-phone a { color: #f5c242; font-weight: 600; }
    @media (max-width: 820px), (hover: none) { .sl-hero-phone { display: block; } }
    .sl-hero-note { margin-top: 0.9rem; font-size: 0.82rem; color: rgba(255,255,255,0.7); }
    .sl-hero-photo { background: #0f5a52 url('/static/home-hero.jpg') 68% center / cover no-repeat; min-height: 320px; }
    @media (max-width: 820px) {
      .sl-hero { grid-template-columns: 1fr; }
      .sl-hero-photo { order: -1; min-height: 240px; }
      .sl-hero-panel { padding: 2.25rem 1.5rem 2.5rem; }
      .sl-hero-panel h1 { font-size: 2rem; }
    }
    /* Calm still-life rows (image on cream + plain headline + outlined button) */
    .sl-row { display: grid; grid-template-columns: 1fr 1fr; gap: 2.5rem; align-items: center; margin: 0 0 2.25rem; text-align: left; }
    .sl-row.reverse .sl-media { order: 2; }
    .sl-media { background: #fff6dc; aspect-ratio: 16 / 11; overflow: hidden; }
    .sl-media img { width: 100%; height: 100%; object-fit: cover; display: block; }
    .sl-copy .steps-kicker { text-align: left; }
    .sl-copy h2 { font-size: 1.7rem; font-weight: 500; letter-spacing: -0.01em; line-height: 1.25; margin: 0 0 0.75rem; color: var(--text); }
    .sl-copy p { color: var(--text-muted); line-height: 1.6; margin: 0 0 1.5rem; font-size: 0.98rem; }
    .sl-outline { display: inline-block; border: 2px solid #0a3f3a; color: #0a3f3a; font-weight: 700; font-size: 0.92rem; padding: 0.75rem 1.4rem; border-radius: 4px; text-decoration: none; transition: background 0.18s ease, color 0.18s ease; }
    .sl-outline:hover { background: #0a3f3a; color: #fff; }
    @media (max-width: 760px) {
      .sl-row { grid-template-columns: 1fr; gap: 1.25rem; }
      .sl-row.reverse .sl-media { order: 0; }
    }

    /* Main column */
    .main { max-width: 840px; margin: 0 auto; padding: 0 2rem; position: relative; z-index: 1; }
    .section { padding-top: 4.5rem; }

    /* Hero */
    .hero { padding-top: 3.25rem; padding-bottom: 1rem; }
    .icon-circle {
      width: 52px; height: 52px; border-radius: 999px;
      background: var(--accent-tint); border: 1px solid rgba(11,93,82,0.18);
      display: flex; align-items: center; justify-content: center;
      margin-bottom: 1.5rem; color: var(--card-dark);
    }
    .hero .check-title {
      font-size: 2.1rem;
      font-weight: 800;
      line-height: 1.1;
      letter-spacing: -0.025em;
      max-width: 620px;
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
      box-shadow: 0 0 0 3px rgba(11,93,82,0.12);
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
    .hero-phone a { color: #0a3a33; font-weight: 600; text-decoration: none; }
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
    .drop-zone { border: 1.5px dashed #d9dee3; border-radius: 16px; background: #fff;
      padding: 2.75rem 1.5rem 2.5rem; text-align: center; cursor: pointer;
      transition: border-color 0.18s ease, background 0.18s ease, box-shadow 0.18s ease; }
    .drop-zone:hover { border-color: var(--border-hover); background: #fbfdfc; }
    .drop-zone.drag { border-style: solid; border-color: var(--accent); background: var(--accent-tint); box-shadow: 0 0 0 4px var(--accent-glow); }
    /* Children would otherwise fire dragleave on the zone as the cursor crosses them (flicker) */
    .drop-zone.drag * { pointer-events: none; }
    .dz-icon { width: 56px; height: 56px; margin: 0 auto 1.1rem; border-radius: 14px; background: #f5c242;
      display: flex; align-items: center; justify-content: center; color: #0a3f3a; transition: background 0.18s ease, color 0.18s ease; }
    .drop-zone:hover .dz-icon, .drop-zone.drag .dz-icon { background: #0b5d52; color: #f5c242; }
    .drop-zone .dz-title { font-weight: 600; font-size: 1rem; color: var(--text); margin-bottom: 0.35rem; }
    .drop-zone .dz-sub { color: var(--text-muted); font-size: 0.85rem; }
    .drop-zone .dz-sub u { text-decoration-color: rgba(11,93,82,0.35); text-underline-offset: 3px; color: var(--accent); }
    .drop-zone .dz-touch { display: none; }
    @media (hover: none) { .drop-zone .dz-mouse { display: none; } .drop-zone .dz-touch { display: inline; } }
    .drop-zone .dz-meta { color: var(--text-dim); font-size: 0.75rem; margin-top: 0.85rem; }
    .dz-trust { display: flex; justify-content: center; flex-wrap: wrap; gap: 0.4rem 1.25rem; margin-top: 0.9rem;
      font-size: 0.8rem; color: var(--text-muted); }
    .dz-trust span { display: inline-flex; align-items: center; gap: 0.35rem; }
    .dz-trust svg { color: var(--accent); flex-shrink: 0; }
    .demo-check-btn { display: block; width: 100%; margin-top: 1.1rem; padding: 0.8rem 1rem; background: #fff;
      border: 1px solid #d9dee3; border-radius: 12px; font: inherit; font-size: 0.9rem; font-weight: 600;
      color: var(--text); cursor: pointer; text-align: center; transition: var(--transition); }
    .demo-check-btn:hover { border-color: var(--accent); color: var(--accent); box-shadow: 0 4px 14px var(--accent-glow); }
    #homeResult { scroll-margin-top: 90px; }
    .demo-check-hint { text-align: center; font-size: 0.78rem; color: var(--text-dim); margin-top: 0.4rem; }

    .demo-check-btn:disabled { opacity: 0.6; cursor: default; }
    .demo-banner { font-size: 0.78rem; font-weight: 700; letter-spacing: 0.02em; color: var(--text-dim);
      text-transform: uppercase; margin-bottom: 0.5rem; }
    .privacy-note { display: flex; align-items: center; gap: 0.45rem; margin-top: 0.9rem; font-size: 0.78rem; color: var(--text-dim); }
    .privacy-note svg { flex-shrink: 0; }
    .or-divider { display: flex; align-items: center; gap: 0.75rem; margin: 0.15rem 0; font-size: 0.68rem; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; color: var(--text-dim); }
    .or-divider::before, .or-divider::after { content: ""; flex: 1; height: 1px; background: var(--border); }
    .email-forward-note { display: flex; align-items: center; gap: 0.6rem; font-size: 0.82rem; color: var(--text-muted); background: var(--accent-tint); border-radius: var(--radius-sm); padding: 0.8rem 1rem; }
    .email-forward-note svg { flex-shrink: 0; color: var(--text-dim); }
    .email-forward-note a { color: var(--accent); font-weight: 700; text-decoration: underline; text-underline-offset: 2px; }
    .email-optin { margin-top: 0.85rem; display: flex; flex-direction: column; gap: 0.5rem; }
    .email-optin-check { display: flex; align-items: center; gap: 0.5rem; font-size: 0.82rem; color: var(--text-muted); cursor: pointer; }
    .email-optin-check input { width: auto; }
    .email-optin-input { padding: 0.6rem 0.85rem; border: 1px solid rgba(15,31,47,0.16); border-radius: var(--radius-sm); font-family: inherit; font-size: 0.85rem; background: #fff; color: var(--text); }
    .email-optin-input:focus { outline: none; border-color: var(--accent); }
    .email-optin-confirm { font-size: 0.78rem; color: #047857; display: none; }
    .email-optin-confirm.show { display: block; }
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
    .individual-upsell { margin-top: 1.25rem; padding: 1.1rem 1.25rem; background: var(--accent-tint); border: 1px solid var(--border); border-radius: var(--radius-sm); }
    .individual-upsell-lead { font-size: 0.85rem; color: var(--text); margin: 0 0 0.85rem; line-height: 1.5; }
    .individual-upsell-btn { background: var(--accent); color: #fff; border: none; padding: 0.7rem 1.25rem; border-radius: 9999px; font: inherit; font-size: 0.85rem; font-weight: 700; cursor: pointer; }
    .individual-upsell-btn:hover { opacity: 0.9; }
    .individual-upsell-alt { margin-top: 0.65rem; font-size: 0.78rem; color: var(--text-muted); }
    .individual-upsell-alt a { color: var(--accent); font-weight: 600; text-decoration: underline; text-underline-offset: 2px; }

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
    @media (min-width: 601px) {
      .stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 1.5rem; width: 100%; max-width: 540px; box-sizing: border-box; }
    }
    /* Center the upload column so it lines up with the centered sections below it. */
    #check { max-width: 620px; margin-left: auto; margin-right: auto; }
    /* No one-word last lines on headings. */
    h1, h2, h3 { text-wrap: balance; }
    /* 7 workflow cards: center the last row instead of stranding card 7 on the left. */
    @media (min-width: 701px) {
      #workflow .steps-grid { display: flex; flex-wrap: wrap; justify-content: center; }
      #workflow .step-card { flex: 0 1 calc((100% - 2.5rem) / 3); }
    }
    .stat-label { font-size: 0.72rem; color: var(--text-dim); margin-top: 0.25rem; font-weight: 500; }

    /* Dark card wrap (dashboard/results preview) -- layered ambient shadow */
    .dark-card-wrap {
      margin-top: 2.75rem;
      position: relative;
      border-radius: 2.25rem;
      background: var(--card-dark);
      padding: 10px;
      border: 1px solid rgba(255,255,255,0.08);
      box-shadow: 0 2px 8px rgba(15,31,47,0.10), 0 12px 40px rgba(15,31,47,0.14);
      overflow: hidden;
    }
    .dark-card-inner {
      position: relative;
      border-radius: 1.75rem;
      background: linear-gradient(180deg, var(--card-dark-2) 0%, var(--card-dark-3) 50%, var(--card-dark) 100%);
      padding: 1.5rem;
      overflow: hidden;
    }
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
    .steps-header h2 { font-size: 2rem; font-weight: 800; margin: 0 0 0.5rem; letter-spacing: -0.02em; line-height: 1.15; }
    .steps-header p { color: var(--text-dim); font-size: 1rem; }
    .steps-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1.25rem; }
    @media (min-width: 961px) {
      #how .steps-grid { grid-template-columns: repeat(4, 1fr); }
    }
    @media (min-width: 701px) {
      #records .steps-grid { grid-template-columns: repeat(2, 1fr); max-width: 760px; margin: 0 auto; }
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
      color: #0a3a33;
      border-radius: var(--radius-sm);
      display: flex; align-items: center; justify-content: center;
      font-weight: 700;
      font-size: 0.85rem;
      margin-bottom: 1.1rem;
    }
    .step-card h3 { font-size: 1.05rem; font-weight: 700; margin: 0 0 0.5rem; letter-spacing: -0.01em; }
    /* "3 ways to get started" + guide cards + support block (2026-10-07) */
    .path-card { display: flex; flex-direction: column; background: #f5c242; border-color: #e6b230; }
    .path-card:hover { border-color: #d9a520; }
    .step-card.path-card p, .step-card.path-card li { color: #2b2615; }
    .path-card .path-kicker { font-size: 0.7rem; font-weight: 700; color: #0a3f3a; text-transform: uppercase; letter-spacing: 0.07em; margin-bottom: 0.5rem; }
    .path-card ul { list-style: none; margin: 0.9rem 0 1.25rem; padding: 0; }
    .path-card li { font-size: 0.85rem; color: var(--text-muted); padding: 0.3rem 0 0.3rem 1.3rem; position: relative; }
    .path-card li::before { content: "\\2713"; position: absolute; left: 0; color: #0a3f3a; font-weight: 700; }
    .path-btn { margin-top: auto; display: block; text-align: center; padding: 0.7rem 1rem; border-radius: 9999px; font-weight: 600; font-size: 0.9rem; text-decoration: none; transition: var(--transition); }
    .path-btn.solid { background: var(--accent); color: #fff; }
    .path-btn.solid:hover { background: var(--accent-light); }
    .path-btn.outline { border: 1.5px solid #0a3f3a; color: #0a3f3a; background: rgba(255,255,255,0.35); }
    .path-btn.outline:hover { background: rgba(255,255,255,0.7); }
    a.guide-card { display: flex; flex-direction: column; text-decoration: none; color: inherit; }
    a.guide-card .guide-tag { font-size: 0.7rem; font-weight: 700; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.07em; margin-bottom: 0.6rem; }
    a.guide-card .guide-more { margin-top: auto; padding-top: 1rem; font-size: 0.85rem; font-weight: 600; color: var(--accent); }
    .support-box { max-width: 760px; margin: 0 auto; background: #fff; border: 1px solid var(--border); border-radius: var(--radius); padding: 2rem; display: grid; grid-template-columns: repeat(3, 1fr); gap: 1.5rem; }
    .support-box h3 { font-size: 0.95rem; font-weight: 700; margin: 0 0 0.35rem; }
    .support-box p { font-size: 0.85rem; color: var(--text-muted); margin: 0 0 0.5rem; }
    .support-box a { font-size: 0.85rem; font-weight: 600; color: var(--accent); }
    @media (max-width: 700px) { .support-box { grid-template-columns: 1fr; padding: 1.5rem; } }
    .step-card p { font-size: 0.87rem; color: var(--text-muted); line-height: 1.55; margin: 0; }
    .step-caption { font-size: 0.75rem; color: var(--text-dim); margin-top: 0.5rem; }

    .tc-checklist { list-style: none; margin: 0 auto; padding: 0; max-width: 640px; text-align: left; }
    .tc-checklist li {
      display: flex; align-items: flex-start; gap: 0.65rem;
      padding: 0.65rem 0; border-bottom: 1px solid var(--border);
      font-size: 0.88rem; color: var(--text-muted); line-height: 1.5;
    }
    .tc-checklist li:last-child { border-bottom: none; }
    .tc-checklist .tc-check {
      flex: 0 0 auto; width: 1.1rem; color: var(--accent-dark); font-weight: 800; line-height: 1.5;
    }
    .tc-checklist strong { color: var(--text); font-weight: 700; }

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
    .footer-copy { color: var(--text-dim); font-size: 0.8rem; }

    @media (max-width: 700px) {
      /* The ambient orbs/gradient are fixed-px sized, tuned to read as a subtle
         accent against a wide desktop viewport -- at mobile widths those same
         pixel sizes cover most of the screen and read as a dominant wash
         instead, so scale everything down here. */
      body {
        background:
          radial-gradient(ellipse 90% 260px at 50% -60px, rgba(15,31,47,0.05) 0%, transparent 55%),
          var(--bg);
      }
      body::before { width: 200px; height: 200px; top: -60px; right: 0; filter: blur(50px); }
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
    .nav-cta { white-space: nowrap; }
    @media (min-width: 701px) and (max-width: 960px) {
      .nav-toggle { display: flex; }
      .nav-links {
        display: none; position: absolute; top: 100%; left: 0; right: 0;
        flex-direction: column; gap: 0; padding: 0.5rem 2rem 1.25rem;
        background: #fff; border-bottom: 1px solid rgba(15,31,47,0.08);
      }
      .nav-links.open { display: flex; }
      .nav-links a { padding: 0.75rem 0; border-bottom: 1px solid rgba(15,31,47,0.08); }
      .nav-links a:last-child { border-bottom: none; }
    }
    @media (max-width: 960px) {
      .nav-cta { margin-left: auto; margin-right: 0.75rem; }
    }
    @media (max-width: 480px) {
      .nav-cta { display: none; }
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
      <img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="height:26px;width:auto;display:block;">
    </a>
    <div class="nav-links" id="navLinks">
      <a href="#how">How it works</a>
      <a href="/brokers">For Brokers</a>
      <a href="#workflow">Workflow</a>
      <a href="/pricing">Pricing</a>
      <a href="/faq">FAQ</a>
      <a href="/tc-hub">TC Hub</a>
      <a href="/login">Log In</a>
    </div>
    <a href="/tc-check" class="nav-cta">Try TC Check Free</a>
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

  <section class="sl-hero">
    <div class="sl-hero-panel">
      <h1>Catch what title kicks back &mdash; before you send the file.</h1>
      <p>Drop a filled TREC 20-19 and see exactly what title would kick back &mdash; blank dates, missing initials, mismatched checkboxes &mdash; in seconds.</p>
      <div class="sl-hero-actions">
        <a class="sl-cta" href="#check" data-evt="hero_cta">Check a file free</a>
        <button type="button" class="sl-cta-secondary" id="heroSampleBtn" data-evt="hero_sample">See a sample report &rarr;</button>
      </div>
      <div class="sl-hero-note">No signup &middot; Checked, then deleted &mdash; no copy kept</div>
      <div class="sl-hero-phone">On your phone? Forward the contract email to <a href="mailto:tc@check.txtanoffer.com">tc@check.txtanoffer.com</a> &mdash; the report comes back by email.</div>
    </div>
    <div class="sl-hero-photo" role="img" aria-label="A father and daughter painting a room in their new home"></div>
  </section>

  <div class="main">
  <section class="hero section" id="check">
    <h2 class="check-title">Drop a TREC 20-19. See exactly what&rsquo;s missing.</h2>
    <p style="font-size:0.85rem;color:var(--accent-dark);font-weight:600;margin-top:0.6rem;">Built to help TCs catch what's missing &mdash; not to replace what you do.</p>

    <div class="input-card">
      <div class="input-label">Try it now &mdash; no signup required</div>
      <div class="drop-zone" id="homeDropZone">
        <div class="dz-icon"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 13v8"/><path d="m8 17 4-4 4 4"/><path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3"/></svg></div>
        <div class="dz-title"><span class="dz-mouse">Drop your contract PDF here</span><span class="dz-touch">Choose your contract PDF</span></div>
        <div class="dz-sub"><span class="dz-mouse">or <u>click to browse</u></span><span class="dz-touch"><u>Tap to browse files</u></span></div>
        <div class="dz-meta">PDF only &middot; TREC 20-19, with or without the 40-11 addendum</div>
      </div>
      <input type="file" id="homeFileInput" accept="application/pdf">
      <div class="dz-trust">
        <span><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>Not kept unless your brokerage archives it</span>
        <span><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>Free, no signup</span>
        <span><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/></svg>Results in seconds</span>
      </div>
      <div class="status" id="homeStatus"></div>
      <div class="result" id="homeResult"></div>
      <button type="button" class="demo-check-btn" id="homeDemoBtn">No PDF handy? Run a sample check &rarr;</button>
      <div class="demo-check-hint">See a real report on a sample TREC contract in 2 seconds.</div>
      <div class="email-optin">
        <label class="email-optin-check"><input type="checkbox" id="homeEmailOptinCheckbox" checked> Email me this report + future checks for this address</label>
        <input type="email" id="homeEmailOptinInput" class="email-optin-input" placeholder="you@example.com" autocomplete="email">
        <div class="email-optin-confirm" id="homeEmailOptinConfirm"></div>
      </div>
      <div class="or-divider">or</div>
      <div class="email-forward-note"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m2 7 10 6 10-6"/></svg>Already have it in your inbox? Forward it to <a href="mailto:tc@check.txtanoffer.com">tc@check.txtanoffer.com</a></div>
      <div class="privacy-note">&#9203;&nbsp; Takes a minute now &mdash; saves you a callback from title tonight.</div>
    </div>

    <div class="stats" style="margin:2.25rem 0 0;">
      <div><div class="stat-num">__TC_STAT1_NUM__</div><div class="stat-label">__TC_STAT1_LABEL__</div></div>
      <div><div class="stat-num">__TC_STAT2_NUM__</div><div class="stat-label">__TC_STAT2_LABEL__</div></div>
      <div><div class="stat-num">__TC_STAT3_NUM__</div><div class="stat-label">__TC_STAT3_LABEL__</div></div>
    </div>
  </section>

  <section class="steps" id="start" style="border-top:none;padding-top:2.5rem;padding-bottom:2.5rem;">
    <div class="steps-header" style="margin-bottom:1.75rem;">
      <div class="steps-kicker">Pick what fits</div>
      <h2>3 ways to get started</h2>
      <p>Check one file on your own, get a walkthrough, or set it up for your whole brokerage.</p>
    </div>
    <div class="steps-grid">
      <div class="step-card path-card">
        <div class="path-kicker">Do it yourself</div>
        <h3>Check a file now</h3>
        <p>Drop one filled TREC 20-19 and see what title would kick back.</p>
        <ul><li>Free, no signup</li><li>Results in seconds</li><li>The file isn&rsquo;t kept</li></ul>
        <a class="path-btn solid" href="/tc-check" data-evt="path_self_cta">Check a file free</a>
      </div>
      <div class="step-card path-card">
        <div class="path-kicker">Talk to us</div>
        <h3>Schedule a walkthrough call</h3>
        <p>A short call to walk through what TC Check catches, using your own files or the sample.</p>
        <ul><li>15 minutes, by phone or video</li><li>Direct answers, no call center</li><li>No sales pitch</li></ul>
        <a class="path-btn outline" href="mailto:support@txtanoffer.com?subject=Schedule%20a%20walkthrough%20call" data-evt="path_walkthrough_cta">Schedule a call</a>
      </div>
      <div class="step-card path-card">
        <div class="path-kicker">For your whole brokerage</div>
        <h3>Set up your roster</h3>
        <p>Every agent&rsquo;s offer checked before it goes out, and records kept for you.</p>
        <ul><li>Start with a free audit of 20 closed files</li><li>5-year archive, searchable by address</li><li>Agents join with one text</li></ul>
        <a class="path-btn outline" href="/brokers" data-evt="path_brokerage_cta">Brokerage option</a>
      </div>
    </div>
  </section>

  <section class="steps" id="brokers" style="border-top:none;padding-top:1rem;padding-bottom:2.5rem;">
    <div class="sl-row">
      <div class="sl-media"><img src="/static/home-desk.jpg" alt="A transaction coordinator working calmly from a home office" style="object-position:62% center;" loading="lazy"></div>
      <div class="sl-copy">
        <div class="steps-kicker">For TCs &mdash; in-house, independent, or running a roster</div>
        <h2>A second set of eyes, so you can actually log off.</h2>
        <p>You already catch nearly everything by hand. This is for the one blank that slips past even a careful TC &mdash; because it's your name on the file, whether that's one brokerage or six.</p>
        <a class="sl-outline" href="#check">Check a file free</a>
      </div>
    </div>
    <ul class="tc-checklist">
      <li><span class="tc-check">&check;</span><span><strong>Effective Date left blank</strong> &mdash; the date every other deadline counts from.</span></li>
      <li><span class="tc-check">&check;</span><span><strong>Missing buyer or seller initials</strong> &mdash; easy to miss page-by-page, hard to fix once closed.</span></li>
      <li><span class="tc-check">&check;</span><span><strong>40-11 addendum mismatches</strong> &mdash; loan amount or financing checkbox disagreeing with the contract.</span></li>
      <li><span class="tc-check">&check;</span><span><strong>39-11 amendment mismatches</strong> &mdash; sales price or property address disagreeing with the original contract.</span></li>
      <li><span class="tc-check">&check;</span><span><strong>Your whole closed-file backlog</strong> &mdash; free sample checks up to 20 files at once; a Brokerage join code raises that to 200, no extra charge.</span></li>
      <li><span class="tc-check">&check;</span><span><strong>A text the moment it happens</strong> &mdash; on the Brokerage plan, a real blocker on any agent's file texts you directly, before it ever reaches title.</span></li>
    </ul>
    <div class="secondary-cta" style="margin:1.75rem auto 0;padding-top:1.75rem;max-width:560px;text-align:center;">
      <div class="secondary-cta-label">Running a brokerage or TC team? Get a text the moment any agent's file has a real blocker &mdash; not just an email you have to open.</div>
      <a href="/pricing#brokerage" class="input-btn" style="display:inline-block;text-decoration:none;">See Brokerage pricing &rarr;</a>
      <div style="margin-top:0.85rem;"><a href="/brokers" style="font-size:0.85rem;color:var(--text-muted);text-decoration:underline;text-underline-offset:2px;">Managing broker? Audit your last 20 closed files free &rarr;</a></div>
    </div>
  </section>

  <section class="steps" id="records" style="border-top:none;padding-top:1rem;padding-bottom:2.5rem;">
    <div class="steps-header" style="margin-bottom:1.75rem;">
      <div class="steps-kicker">For managing brokers &mdash; Brokerage plan</div>
      <h2>Every offer your agents text in, archived for 5 years.</h2>
      <p>TREC asks you to keep transaction records for 4 years from closing (22 TAC &sect;535.2). Here's how files land in your archive &mdash; your agents don't do anything extra.</p>
    </div>
    <div class="steps-grid">
      <div class="step-card">
        <div class="step-num">1</div>
        <h3>Agents join your roster</h3>
        <p>Each agent texts your brokerage's join code before their first offer, or enters it at signup. One time, no per-agent setup.</p>
      </div>
      <div class="step-card">
        <div class="step-num">2</div>
        <h3>They text the offer</h3>
        <p>The TREC 20-19 comes back in seconds, gets checked for blanks and mismatches, and a copy goes to your TC by email.</p>
      </div>
      <div class="step-card">
        <div class="step-num">3</div>
        <h3>Changes are filed with it</h3>
        <p>When an agent texts a price or closing-date change (<code>AMEND 123 Main St price 730k</code>), it becomes a 39-11 amendment, archived under the same address.</p>
      </div>
      <div class="step-card">
        <div class="step-num">4</div>
        <h3>Find it in seconds</h3>
        <p>Search by address from your dashboard, open any PDF, or download the whole archive as a ZIP &mdash; anytime, even if you cancel.</p>
      </div>
    </div>
    <p style="text-align:center;font-size:0.8rem;color:var(--text-dim);max-width:620px;margin:1.5rem auto 0;">Executed contracts too: forward them to tc@check.txtanoffer.com from your brokerage email, or drop them into your archive &mdash; each one is checked and kept 5 years. <a href="/archive" style="color:var(--accent-dark);text-decoration:underline;">How the contract archive works &rarr;</a></p>
    <div style="text-align:center;margin-top:1.25rem;"><a class="sl-outline" href="/brokers" data-evt="records_brokers_cta">See it for your brokerage &rarr;</a></div>
  </section>

  <section class="steps" id="workflow" style="border-top:none;padding-top:1rem;padding-bottom:2.5rem;">
    <div class="steps-header" style="margin-bottom:1.75rem;">
      <div class="steps-kicker">The full picture</div>
      <h2>From offer to closing, in one place.</h2>
      <p>TC File Check is the part most people try first. It's one stage of a longer workflow that runs underneath it, start to finish.</p>
    </div>
    <div class="steps-grid">
      <div class="step-card">
        <div class="step-num">1</div>
        <h3>Offer</h3>
        <p>Your agents text the terms in and get back a structured offer in seconds &mdash; the starting point everything else builds on, and the first record in your brokerage's archive.</p>
      </div>
      <div class="step-card">
        <div class="step-num">2</div>
        <h3>TREC Documents</h3>
        <p>The required forms are generated straight from the transaction: 20-19, 39-11, 40-11, IABS, 61-0, 36-10.</p>
      </div>
      <div class="step-card">
        <div class="step-num">3</div>
        <h3>Compliance &mdash; TC Check</h3>
        <p>Checked for completeness, cross-document consistency, and version-to-version comparison &mdash; before it moves forward.</p>
      </div>
      <div class="step-card">
        <div class="step-num">4</div>
        <h3>Signature execution</h3>
        <p>Connected to DocuSign for e-signature. Validation-gated &mdash; a contract that's still missing required fields won't go out.</p>
      </div>
      <div class="step-card">
        <div class="step-num">5</div>
        <h3>Communication</h3>
        <p>SMS confirmations, email notifications, and a listing-agent accept/decline thread, built in from the first text.</p>
      </div>
      <div class="step-card">
        <div class="step-num">6</div>
        <h3>Transaction workspace</h3>
        <p>Once accepted, a stage timeline and task checklist track the deal &mdash; option period, earnest money, title, financing, inspections, amendments, final walkthrough &mdash; alongside accept/decline status and dashboard visibility.</p>
      </div>
      <div class="step-card">
        <div class="step-num">7</div>
        <h3>Closing</h3>
        <p>Within 5 days of closing, a Closing Checklist pulls it together in one screen: documents complete, signature status, open tasks, and every key date.</p>
      </div>
    </div>
    <div style="text-align:center;font-size:0.8rem;font-weight:600;color:var(--accent-dark);margin-top:1.75rem;">Offer &rarr; Documents &rarr; Compliance &rarr; Signatures &rarr; Communication &rarr; Transaction &rarr; Closing</div>
  </section>
  </div>

  <section class="steps" id="how">
    <div class="steps-header">
      <h2 style="max-width:620px;margin:0 auto;">Forward it, or drop it here. Get back exactly what's missing.</h2>
    </div>
    <ul class="tc-checklist" style="max-width:560px;margin:1.5rem auto 0;">
      <li><span class="tc-check">&check;</span><span>Forward the TREC 20-19 to <strong>tc@check.txtanoffer.com</strong>, or upload it above &mdash; add the 40-11 addendum or 39-11 amendment if you've got them.</span></li>
      <li><span class="tc-check">&check;</span><span>We re-read the actual filled-in PDF against TREC's current form &mdash; not a guess based on file size or page count.</span></li>
      <li><span class="tc-check">&check;</span><span>Get an itemized report back in under a minute: what's blank, what's missing an initial, what disagrees with the addendum.</span></li>
    </ul>
    <div class="secondary-cta" style="margin:1.75rem auto 0;padding-top:1.75rem;max-width:540px;text-align:center;">
      <div class="secondary-cta-label">Free. No login. Checked, then deleted &mdash; no copy kept.</div>
      <a href="/tc-check" class="input-btn" style="display:inline-block;text-decoration:none;">Try TC File Check &rarr;</a>
    </div>
  </section>

  <section class="steps" id="guides">
    <div class="steps-header">
      <div class="steps-kicker">Free guides</div>
      <h2>Guides for Texas TCs and brokers</h2>
      <p>Plain-language answers to the questions that hold up closings.</p>
    </div>
    <div class="steps-grid">
      <a class="step-card guide-card" href="/guides/trec-20-19-checklist" data-evt="guide_card_cta">
        <div class="guide-tag">For TCs</div>
        <h3>TREC 20-19 checklist before you send the file to title</h3>
        <p>The blanks and mismatches to check, in order.</p>
        <span class="guide-more">Read the guide &rarr;</span>
      </a>
      <a class="step-card guide-card" href="/guides/40-11-loan-amount-mismatch" data-evt="guide_card_cta">
        <div class="guide-tag">For TCs</div>
        <h3>40-11 loan amount doesn&rsquo;t match the contract</h3>
        <p>Section 3B vs. the addendum, and the checkbox mistakes that go with it.</p>
        <span class="guide-more">Read the guide &rarr;</span>
      </a>
      <a class="step-card guide-card" href="/guides/broker-record-retention-texas" data-evt="guide_card_cta">
        <div class="guide-tag">For brokers</div>
        <h3>How long Texas brokers must keep transaction records</h3>
        <p>The 4-year TREC rule and a practical way to keep up with it.</p>
        <span class="guide-more">Read the guide &rarr;</span>
      </a>
    </div>
    <p style="text-align:center;margin-top:1.5rem;font-size:0.9rem;"><a href="/guides/trec-deadline-calculator" style="color:var(--accent);font-weight:600;" data-evt="guide_card_cta">Free TREC deadline calculator &rarr;</a> &nbsp;&middot;&nbsp; <a href="/guides" style="color:var(--accent);font-weight:600;">See all guides &rarr;</a></p>
  </section>

  <section class="steps" id="trust">
    <div class="steps-header">
      <h2>Built so nothing slips through.</h2>
      <p>The anxiety isn't "I wish this were faster" &mdash; it's "did I miss a checkbox," re-checked from your phone after dinner. Here's how we handle that, so you can actually log off.</p>
    </div>
    <div class="steps-grid">
      <div class="step-card">
        <div class="step-num">&check;</div>
        <h3>Every field checked, not just assumed</h3>
        <p>We re-read the actual filled-in PDF itself &mdash; not a guess based on file size or page count &mdash; and flag exactly which required field, checkbox, or dollar amount is missing or inconsistent. The same check, run the same way, on every file.</p>
      </div>
      <div class="step-card">
        <div class="step-num">&check;</div>
        <h3>Checked against TREC's current form</h3>
        <p>Every check is mapped field-by-field against TREC's actual published 20-19 form and re-verified when TREC revises it. <a href="/trec-changes" style="color:var(--text);text-decoration:underline;">See what changed &rarr;</a> &middot; <a href="/tc-hub" style="color:var(--text);text-decoration:underline;">Free TC Hub &rarr;</a></p>
      </div>
      <div class="step-card">
        <div class="step-num">&check;</div>
        <h3>Files you check aren&rsquo;t kept</h3>
        <p>A contract you upload or forward to TC Check is processed for the report, then discarded. If you get the report by email, its subject line includes the property address, and for bulk checks we keep each file&rsquo;s name and issue count, viewable at your batch link. The one exception is on purpose: on the Brokerage plan, contracts your brokerage forwards or uploads to its archive, and offers your agents text in, are kept for your records &mdash; <a href="#records" style="color:var(--accent-dark);text-decoration:underline;">see how that works</a>.</p>
      </div>
    </div>
  </section>

  <section class="steps" id="support">
    <div class="steps-header" style="margin-bottom:1.75rem;">
      <h2>Questions? A real person answers.</h2>
      <p>TxtAnOffer is built and run by one person in Texas. You&rsquo;ll hear back from the person who built it, not a ticket queue.</p>
    </div>
    <div class="support-box">
      <div>
        <h3>Help yourself</h3>
        <p>Common questions about TC Check, storage, and TREC forms.</p>
        <a href="/faq">Read the FAQ &rarr;</a>
      </div>
      <div>
        <h3>Email</h3>
        <p>Questions, feedback, or a walkthrough request.</p>
        <a href="mailto:support@txtanoffer.com" data-evt="support_email_cta">support@txtanoffer.com</a>
      </div>
      <div>
        <h3>Forward a file</h3>
        <p>Send a TREC 20-19 from your inbox and get the report back by email.</p>
        <a href="mailto:tc@check.txtanoffer.com">tc@check.txtanoffer.com</a>
      </div>
    </div>
  </section>

  <footer class="footer">
    <div class="trust-badges">
      <span class="trust-badge">Checked, then deleted</span>
      <span class="trust-badge">Made for Texas TREC forms</span>
      <span class="trust-badge">Billing by Stripe</span>
    </div>
    <div class="footer-links">
      <a href="/about">About</a>
      <a href="/faq">FAQ</a>
      <a href="/contact">Contact</a>
      <a href="/terms">Terms of Service</a>
      <a href="/privacy">Privacy Policy</a>
      <a href="/privacy#sms-messaging">SMS Terms</a>
      <a href="/pricing">Pricing</a>
      <a href="/playground">Try a Sample Text</a>
      <a href="/tc-check">TC File Check</a>
      <a href="/guides">Guides</a>
      <a href="mailto:support@txtanoffer.com">Support</a>
    </div>
    <div class="footer-copy">
      &copy; 2026 TxtAnOffer &middot; Texas, United States &middot; Not affiliated with TREC
    </div>
  </footer>

<script>
(function(){
  var dropZone = document.getElementById('homeDropZone'),
      fileInput = document.getElementById('homeFileInput'),
      statusEl = document.getElementById('homeStatus'),
      resultEl = document.getElementById('homeResult'),
      emailOptinCheckbox = document.getElementById('homeEmailOptinCheckbox'),
      emailOptinInput = document.getElementById('homeEmailOptinInput'),
      emailOptinConfirm = document.getElementById('homeEmailOptinConfirm'),
      demoBtn = document.getElementById('homeDemoBtn');
  if(!dropZone) return;

  try{ var savedEmail = localStorage.getItem('tc_email_hint'); if(savedEmail) emailOptinInput.value = savedEmail; }catch(e){}

  function optinEmail(){
    var v = emailOptinInput.value.trim();
    if(!emailOptinCheckbox.checked || !v) return '';
    try{ localStorage.setItem('tc_email_hint', v); }catch(e){}
    return v;
  }

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

  if(demoBtn){
    var demoLabel = demoBtn.textContent;
    demoBtn.addEventListener('click', function(){
      demoBtn.disabled = true;
      demoBtn.textContent = 'Checking the sample\u2026';
      fetch('/static/sample_trec_20-19.pdf?v=2026-10-09')
        .then(function(r){ return r.blob(); })
        .then(function(blob){
          uploadFile(new File([blob], 'sample_trec_20-19.pdf', {type:'application/pdf'}), true);
        })
        .catch(function(){ demoDone(false); });
    });
  }
  // Hero "See a sample report" runs the same sample check as the button
  // under the drop zone, so phone visitors (who rarely have a TREC PDF on
  // the phone) get a real report without scrolling two screens down first.
  var heroSampleBtn = document.getElementById('heroSampleBtn');
  if(heroSampleBtn && demoBtn){
    var heroSampleLabel = heroSampleBtn.textContent;
    heroSampleBtn.addEventListener('click', function(){
      if(demoBtn.disabled) return;
      heroSampleBtn.disabled = true;
      heroSampleBtn.textContent = 'Checking the sample\u2026';
      demoBtn.click();
    });
  }
  // The result renders ABOVE this button (right under the drop zone), so
  // without this the click looked like a no-op: the button snapped back to
  // its label instantly and scroll anchoring kept the new result off-screen.
  function demoDone(ok){
    if(!demoBtn) return;
    demoBtn.textContent = ok ? '\u2713 Done \u2014 results above' : 'Couldn\u2019t load the sample. Try again \u2192';
    setTimeout(function(){ demoBtn.textContent = demoLabel; demoBtn.disabled = false; }, ok ? 3500 : 2500);
    if(heroSampleBtn){ heroSampleBtn.textContent = heroSampleLabel; heroSampleBtn.disabled = false; }
  }
  function revealResult(){
    var r = resultEl.getBoundingClientRect();
    if(r.top < 70 || r.top > window.innerHeight * 0.6){
      resultEl.scrollIntoView({behavior:'smooth', block:'start'});
    }
  }

  // Same perceived-progress pattern as /tc-check -- one real round trip,
  // staged labels just so the wait doesn't feel dead.
  var STATUS_STEPS = ['Scanning TREC 20-19...', 'Checking mandatory fields...', 'Checking initials & consistency...'];
  var statusTimers = [];

  var fileCarried = false;

  // The click-through to /tc-check for "see the full checklist" used to
  // land on a blank form for a real (non-demo) file -- nothing carries it
  // across a normal navigation, since the file is never sent to our
  // servers a second time. Stashing it as a data URL in sessionStorage
  // (this browser's own storage, read back once on /tc-check and then
  // cleared -- see that page's script) lets the click-through replay the
  // same file without ever storing it server-side. Best-effort: any
  // failure (quota, unsupported) just falls back to today's blank form.
  function stashFileForCarry(file){
    fileCarried = false;
    if(!file || !window.sessionStorage || !window.FileReader) return;
    try{
      var reader = new FileReader();
      reader.onload = function(){
        try{
          sessionStorage.setItem('tc_carry_file', JSON.stringify({
            name: file.name, type: file.type, dataUrl: reader.result
          }));
          fileCarried = true;
        }catch(e){ fileCarried = false; }
      };
      reader.onerror = function(){ fileCarried = false; };
      reader.readAsDataURL(file);
    }catch(e){ fileCarried = false; }
  }

  function uploadFile(file, isDemo){
    resultEl.classList.remove('show');
    emailOptinConfirm.classList.remove('show');
    statusTimers.forEach(clearTimeout);
    statusTimers = STATUS_STEPS.map(function(label, i){
      return setTimeout(function(){ statusEl.textContent = label; }, i * 450);
    });
    statusEl.textContent = STATUS_STEPS[0];
    statusEl.classList.add('show');
    if(!isDemo) stashFileForCarry(file);

    var email = isDemo ? '' : optinEmail();
    var formData = new FormData();
    formData.append('file', file);
    formData.append('source_page', 'homepage');
    if(email) formData.append('email', email);
    if(isDemo) formData.append('is_demo', '1');

    fetch('/v1/tc/check', { method: 'POST', body: formData })
      .then(function(r){ return r.json(); })
      .then(function(data){
        statusTimers.forEach(clearTimeout);
        statusEl.classList.remove('show');
        if(data.error){ renderError(data.error); if(isDemo) demoDone(false); revealResult(); return; }
        renderResult(data, isDemo);
        if(isDemo) demoDone(true);
        revealResult();
        if(email){
          emailOptinConfirm.textContent = 'Sent to ' + email;
          emailOptinConfirm.classList.add('show');
        }
      })
      .catch(function(){
        statusTimers.forEach(clearTimeout);
        statusEl.classList.remove('show');
        renderError('Something went wrong checking that file. Try again.');
        if(isDemo) demoDone(false);
        revealResult();
      });
  }

  function renderError(msg){
    resultEl.innerHTML = '<div class="result-banner incomplete">' + escapeHtml(msg) + '</div>';
    resultEl.classList.add('show');
  }

  function renderResult(data, isDemo){
    var issues = data.issues || [];
    var totalIssues = typeof data.issue_count === 'number' ? data.issue_count : issues.length;
    var html = '';
    if(isDemo){
      html += '<div class="demo-banner">Demo result &mdash; a sample contract with planted mistakes, not your file.</div>';
    }
    if(data.complete){
      html += '<div class="result-banner complete">All checked fields are filled in.</div>';
    } else {
      // Lead with the blocking count -- the concrete "title will reject this"
      // number -- when there is one; fall back to a plain issue count for a
      // file whose only findings are non-blocking warnings.
      var blockerCount = typeof data.blocker_count === 'number' ? data.blocker_count : 0;
      var bannerText = blockerCount > 0
        ? blockerCount + ' title-blocking error' + (blockerCount === 1 ? '' : 's') + ' found'
        : totalIssues + ' issue' + (totalIssues === 1 ? '' : 's') + ' found';
      html += '<div class="result-banner incomplete">' + bannerText + '</div>';
    }
    // Homepage widget shows the first few issues -- the full checklist,
    // email gate, copy/download buttons, and blank-draft CTA live on
    // /tc-check itself.
    var shown = data.gated ? issues : issues.slice(0, 4);
    if(shown.length){
      html += '<ul class="issue-list">';
      shown.forEach(function(issue){
        html += '<li class="issue-item"><span class="issue-tag ' + issue.severity + '">' + issue.severity + '</span><span>' + escapeHtml(issue.message) + '</span></li>';
      });
      html += '</ul>';
    }
    // Demo replays the fixed static sample; a real file replays from the
    // sessionStorage stash above when it succeeded, otherwise this falls
    // back to a plain link (today's "start over" behavior).
    var tcCheckHref = isDemo ? '/tc-check?demo=1' : (fileCarried ? '/tc-check?carry=1' : '/tc-check');
    if(totalIssues > shown.length){
      var linkText = data.gated ? 'enter your email to see exactly which pages and lines' : 'see the full checklist';
      html += '<div class="result-more">+' + (totalIssues - shown.length) + ' more &mdash; <a href="' + tcCheckHref + '">' + linkText + ' &rarr;</a></div>';
    } else if(issues.length){
      html += '<div class="result-more"><a href="' + tcCheckHref + '">Copy or download this checklist &rarr;</a></div>';
    }
    // Self-serve upgrade path for a solo agent/TC drafting these by hand --
    // only once the email gate is already cleared (email on file, or this
    // particular file had nothing to gate), same as /tc-check's own
    // buildUpsellCta(). Never shown alongside the gate itself: the gate's
    // one job is getting an email, and a second ask right next to it would
    // just split attention. Since 2026-10-02 the card leads with the free
    // 3-offer trial (/signup) instead of $40/mo checkout -- the trial is
    // the step that was getting zero traffic from TC Check.
    if(!isDemo && !data.gated && totalIssues > 0){
      html += buildIndividualUpgradeCard();
    }
    resultEl.innerHTML = html;
    resultEl.classList.add('show');
  }

  function buildIndividualUpgradeCard(){
    return '<div class="individual-upsell">'
      + '<p class="individual-upsell-lead">Filling this out by hand? TxtAnOffer drafts a TREC 20-19 by text message instead &mdash; address, price, and closing date auto-fill correctly, so there\\'s nothing left for a check like this to catch.</p>'
      + '<a href="/signup" class="individual-upsell-btn" style="display:block;text-align:center;text-decoration:none;">Draft your next offer free &mdash; 3 offers, no card &rarr;</a>'
      + '<div class="individual-upsell-alt">Already sold? <a href="/pricing">See plans &rarr;</a></div>'
      + '</div>';
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
    # Carry ?src=name (e.g. from a Direct Reach email) through the "Try TC
    # Check Free" CTA to /tc-check, so a click through this exact nav
    # button still attributes correctly even though it lands on the
    # homepage first -- see get_landing_visits_by_source() on /analytics.
    import re as _re
    src = _re.sub(r"[^a-zA-Z0-9_-]", "", request.args.get("src", ""))[:60]
    if src:
        html = html.replace(
            '<a href="/tc-check" class="nav-cta">Try TC Check Free</a>',
            f'<a href="/tc-check?src={src}" class="nav-cta">Try TC Check Free</a>',
            1,
        )
    # Real TC File Check production numbers, not marketing copy -- swapped
    # in here instead of hardcoded so the homepage never goes stale, and
    # never a "fabricated" stat because a sample too small to be honest
    # (see MIN_SAMPLE) falls back to plain, factual generic copy instead.
    MIN_SAMPLE = 5
    tc_stats = get_tc_check_summary(days=30)
    if PUBLIC_TC_STATS_ENABLED and tc_stats["recognized"] >= MIN_SAMPLE:
        pct_incomplete = round(100 - tc_stats["completion_rate"], 1)
        top_issue = tc_stats["issue_frequency"][0] if tc_stats["issue_frequency"] else None
        stat1_num, stat1_label = str(tc_stats["recognized"]), "TREC 20-19 files scanned by TC File Check (last 30 days)"
        stat2_num, stat2_label = f"{pct_incomplete:g}%", "arrived with at least one blank required field"
        if top_issue:
            stat3_num, stat3_label = f"{top_issue['pct_of_recognized']:g}%", f"Top issue: {top_issue['label']}"
        else:
            stat3_num, stat3_label = f"{pct_incomplete:g}%", "had at least one issue TC File Check catches automatically"
    else:
        stat1_num, stat1_label = "Free", "for every Texas TC &mdash; no signup required"
        stat2_num, stat2_label = "Seconds", "to scan a full TREC 20-19 + 40-11 addendum"
        stat3_num, stat3_label = "0", "checked PDFs kept after your report"
    for token, value in (
        ("__TC_STAT1_NUM__", stat1_num), ("__TC_STAT1_LABEL__", stat1_label),
        ("__TC_STAT2_NUM__", stat2_num), ("__TC_STAT2_LABEL__", stat2_label),
        ("__TC_STAT3_NUM__", stat3_num), ("__TC_STAT3_LABEL__", stat3_label),
    ):
        html = html.replace(token, value)

    resp = make_response(html)
    # First-touch attribution cookie: the query-param rewrite above only
    # survives if the visitor clicks "Try TC Check Free" in this exact page
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
    track_page_view(resp, "homepage")
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


# Short field labels for the SMS alert -- tc_check_email.py has its own
# _CONSEQUENCE_TAGS for the fuller email report, but those are sentence-length
# ("TITLE KICKBACK RISK"); an SMS needs to fit several field names in one
# 160-char segment, so this is deliberately terser. Keys match tc_audit.py's
# CHECKED_FIELDS + the initials/effective-date keys it also emits.
_SMS_SHORT_FIELD_LABELS = {
    "address": "Address",
    "city": "City",
    "county": "County",
    "buyer_name": "Buyer Name",
    "seller_name": "Seller Name",
    "escrow_agent_name": "Escrow Agent",
    "earnest_money_amount": "Earnest Money",
    "option_fee_amount": "Option Fee",
    "title_company": "Title Co",
    "effective_date": "Effective Date",
    "initials_buyer": "Buyer Initials",
    "initials_seller": "Seller Initials",
    "sales_price_math": "Price Math",
    "closing_date_invalid": "Closing Date",
    "option_days_blank": "Option Days",
    "check_one_conflict": "Checkbox Conflict",
    "disclosure_days_blank": "Disclosure Days",
    "broker_contribution_incomplete": "Broker Contribution",
    "receipt_mismatch": "Receipt Amount",
    "header_address": "Page Header",
    "header_address_blank": "Page Header",
    "financing_addendum_missing": "Financing Addendum",
    "closing_before_effective": "Closing Date",
    "disclosure_box_missing": "Seller's Disclosure",
    "initials_mismatch": "Wrong Initials",
}


def _notify_brokerage_tc(user: dict, draft: dict, pdf_path: str, pdf_url: str):
    """The 'trojan horse' pipeline: an agent linked to a brokerage (see
    extract_brokerage_prefix) never has to do anything else -- every offer
    they text in auto-CCs that brokerage's TC with the finished PDF and a
    quick compliance read, straight into an inbox they already check.
    Silent no-op for any agent not linked to a brokerage, and never lets a
    failure here (bad email, audit engine hiccup) block the agent's own
    SMS reply -- this is a value-add on top of the core flow, not a gate.

    Also texts brokerage["tc_phone"] when set (Brokerage Alert SMS): same
    trigger, but only on a BLOCKER-level issue (not every warning-only
    file) -- a broker's phone buzzing on every minor gap trains them to
    ignore it, same reasoning as tc_audit.py's own blocking/non-blocking
    split. Email keeps going regardless of whether tc_phone is set."""
    brokerage_id = user.get("brokerage_id") if user else None
    if not brokerage_id:
        return
    try:
        brokerage = get_brokerage(brokerage_id)
        if not brokerage or not brokerage.get("tc_email"):
            return
        try:
            audit = check_tc_file([pdf_path])
            issues = audit.get("issues") or []
            blockers = [i for i in issues if i.get("severity") == "blocker"]
            if issues:
                audit_line = f"TC File Check flagged {len(issues)} item(s) to review before this goes out."
            else:
                audit_line = "TC File Check found nothing missing on the fields it can verify."
        except Exception:
            issues, blockers = [], []
            audit_line = "(TC File Check couldn't scan this file automatically -- worth a manual look.)"

        email_result = send_plain_email(
            brokerage["tc_email"],
            f"New offer drafted: {draft.get('address', 'address unknown')}",
            (
                f"An agent on your roster just drafted an offer through TxtAnOffer.\n\n"
                f"Address: {draft.get('address', 'n/a')}\n"
                f"Price: ${draft.get('price', 0):,}\n\n"
                f"PDF: {pdf_url}\n\n"
                f"{audit_line}\n\n"
                f"This is an automatic notification for {brokerage['name']} -- the agent still "
                f"reviews and sends the offer themselves; nothing here is sent on their behalf."
            ),
        )
        email_sent = bool((email_result or {}).get("success"))

        # sms_eligible: would this notify have texted if tc_phone were set --
        # i.e. there's a real blocker to alert on. Tracked even when
        # tc_phone is blank so brokerage_alert_delivery() can tell "this
        # brokerage would benefit from adding a TC phone" apart from "this
        # brokerage's files just haven't had a blocker yet".
        sms_eligible = bool(blockers)
        sms_sent = False
        if brokerage.get("tc_phone") and sms_eligible:
            agent_profile = get_agent_profile(user.get("phone", "")) or {}
            agent_label = agent_profile.get("name") or user.get("phone", "an agent")
            labels = [_SMS_SHORT_FIELD_LABELS.get(i.get("key"), i.get("key", "field")) for i in blockers]
            shown, extra = labels[:3], len(labels) - 3
            missing = ", ".join(shown) + (f" +{extra} more" if extra > 0 else "")
            # Plain hyphens, not em-dashes -- keeps the message inside GSM-7
            # so it's one Twilio segment instead of silently falling into
            # UCS-2 (70 chars/segment) purely from a non-ASCII separator.
            sms_sent = twilio_send_sms(
                brokerage["tc_phone"],
                f"{agent_label} - {draft.get('address', 'address unknown')} - "
                f"Missing: {missing} - {pdf_url}",
            )

        # One event per notify, denormalized with the brokerage's own
        # source/tc_phone state rather than requiring a later join --
        # see get_brokerage_alert_delivery() in analytics.py, added
        # 2026-09-11 specifically to answer "are the alert-SMS-eligible
        # brokerages (has tc_phone) actually getting texted, and which
        # ?src= campaign brought them in" (e.g. zillow_broker_reach).
        track_event("brokerage_alert_sent", user.get("phone"), {
            "brokerage_id": brokerage_id,
            "source": brokerage.get("source") or "direct",
            "has_tc_phone": bool(brokerage.get("tc_phone")),
            "email_sent": email_sent,
            "sms_eligible": sms_eligible,
            "sms_sent": bool(sms_sent),
            "blocker_count": len(blockers),
        })
    except Exception as e:
        print(f"[BROKERAGE_TC] notify failed for brokerage {brokerage_id}: {e}")


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
                f"$40/mo, cancel anytime"
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
    _notify_brokerage_tc(user, draft, pdf_path, pdf_url)

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

        # A brokerage's join_code leading the message ("KW123 725k 3% 21day
        # 104 Main St") binds this phone to that brokerage permanently on
        # first use -- no separate registration step for the agent. Strip
        # it before any parsing sees the message; a code that doesn't match
        # a real brokerage is left alone and parsed as normal (most likely
        # fails as an unrecognized address, same as today).
        brokerage_hit, incoming_msg = extract_brokerage_prefix(incoming_msg)
        if brokerage_hit and user.get("brokerage_id") != brokerage_hit["id"]:
            link_user_to_brokerage(agent_phone, brokerage_hit["id"])
            track_event("brokerage_linked", agent_phone, {
                "brokerage_id": brokerage_hit["id"],
                "brokerage_name": brokerage_hit["name"],
            })

        if not can_generate:
            track_event("limit_reached", agent_phone)
            payment_url = request.host_url.rstrip("/") + "/pricing"
            twilio_send_sms(agent_phone,
                f"You've used your {FREE_OFFER_LIMIT} free offers!\n"
                f"Subscribe for unlimited: {payment_url}\n"
                f"$40/mo, cancel anytime"
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
<meta name="description" content="Generate TREC purchase offers in 10 seconds via text or web. A filled TREC 20-19 from a text message.">
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
    --accent: #0b5d52;
    --accent-light: #16806e;
    --accent-dark: #0a3a33;
    --accent-tint: #E7F3F1;
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
      <img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="height:26px;width:auto;display:block;">
    </a>
    <div class="nav-links" id="navLinks">
      <a href="/#how">How it works</a>
      <a href="/tc-hub">TC Hub</a>
      <a href="/pricing">Pricing</a>
      <a href="/faq">FAQ</a>
      <a href="/login">Log In</a>
    </div>
    <a href="/tc-check" class="nav-cta">Try TC Check Free</a>
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
    <p class="page-sub">Preparing a purchase offer by hand takes time. TxtAnOffer drafts one in under 10 seconds.</p>

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
        <input type="text" name="offer_text" placeholder="725k 3% 21day Harris 123 Main St" value="{prefill}">
        <button type="submit">Generate My Contract</button>
        <div class="hint">price &middot; down % &middot; closing days &middot; county (optional) &middot; address &middot; financing type &amp; inspection days (optional)</div>
        <div class="hint">You'll get a confirmation to review first &mdash; reply <code>YES</code> to generate the PDF, <code>NO</code> to cancel, or send corrections.</div>
        <div class="hint">Already sent one? Amend it: <code>AMEND 123 Main St price 730k</code> or <code>AMEND 123 Main St close +10</code></div>
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
      <div class="trust-item"><div class="trust-val">TREC</div><div class="trust-label">Official 20-19 form</div></div>
      <div class="trust-item"><div class="trust-val">HTTPS</div><div class="trust-label">Encrypted in transit</div></div>
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
                <span class="share-label">Draft an offer by text</span>
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


# Short chip-style labels for the anonymous gate's category teaser --
# distinct from analytics.TC_ISSUE_LABELS, which is full-sentence and built
# for a stats table row, not a compact "including X, Y, Z" phrase.
TC_GATE_CATEGORY_LABELS = {
    "address": "Property Address",
    "city": "City",
    "county": "County",
    "buyer_name": "Buyer Name",
    "seller_name": "Seller Name",
    "escrow_agent_name": "Escrow Agent",
    "earnest_money_amount": "Earnest Money",
    "option_fee_amount": "Option Fee",
    "title_company": "Title Company",
    "effective_date": "Effective Date",
    "initials_buyer": "Buyer Initials",
    "initials_seller": "Seller Initials",
    "loan_amount_mismatch": "Loan Amount Mismatch",
    "addendum_checkbox_mismatch": "Financing Checkbox",
    "sales_price_math": "Sales Price Math",
    "closing_date_invalid": "Closing Date",
    "option_days_blank": "Option Period",
    "check_one_conflict": "Checkbox Conflict",
    "disclosure_days_blank": "Seller's Disclosure",
    "broker_contribution_incomplete": "Broker Contribution",
    "receipt_mismatch": "Receipt Amount",
    "header_address": "Page Header Address",
    "header_address_blank": "Page Header Address",
    "financing_addendum_missing": "Financing Addendum",
    "closing_before_effective": "Closing Date",
    "disclosure_box_missing": "Seller's Disclosure",
    "initials_mismatch": "Wrong Initials",
}


@app.route("/v1/tc/check", methods=["POST"])
def tc_check():
    """Transaction-coordinator file audit: upload a TREC 20-19 AcroForm PDF,
    get back which already-rect-verified fields are still blank. No auth in
    v1 (see MAX_CONTENT_LENGTH above for the abuse guard on an open upload
    endpoint) -- see tc_audit.py for exactly what is and isn't checked."""
    # Per-IP throttle: this endpoint shares a Railway service (and worker
    # processes) with the paying SMS product, so an unauthenticated flood
    # here could degrade that too, not just this free tool. Abuse
    # protection, not a product limit -- the check itself is always free.
    client_ip = request.remote_addr or "unknown"
    if not check_and_increment(f"tc_check:{client_ip}", limit=20):
        return jsonify({"error": "Too many requests. Try again in a bit."}), 429

    run_tc_followup_if_due()

    # "Run a demo check on a sample contract" button (homepage + /tc-check)
    # posts here too, flagged via is_demo, so the exact same audit code runs
    # against the same file real visitors get checked against -- an honest
    # demo, not a canned/fabricated result. Kept OUT of every real-usage
    # metric below (tc_check_attempted, tc_check, record_tc_use, email
    # capture/send) for the same reason bulk-check events get their own
    # bucket (see tc_check_bulk_submitted): one file clicked by many curious
    # visitors would otherwise dilute the "X real closed files checked, Y%
    # came back clean" stat that's actually used as a marketing/product
    # claim. Its own 'tc_check_demo_used' event exists purely so demo usage
    # is visible somewhere, not folded into either real metric.
    # Which UI this request actually came from -- homepage widget vs the
    # dedicated /tc-check page -- set explicitly by each page's own JS
    # rather than inferred from the Referer header (routinely missing or
    # stripped by privacy tools/ad blockers, so it can't be trusted as the
    # only source of truth). Added 2026-09-11: before this there was no way
    # to tell whether the homepage's widget converts differently from
    # /tc-check's, only a combined total.
    source_page = request.form.get("source_page") or "unknown"
    if source_page not in ("homepage", "tc_check_page"):
        source_page = "unknown"

    is_demo = request.form.get("is_demo") == "1"
    if is_demo:
        track_event("tc_check_demo_used", metadata={"source": request.cookies.get("ta_src") or "direct", "source_page": source_page,
                                                    "visitor": request.cookies.get("ta_vid", "")})
    else:
        # Fires on every real attempt -- success, wrong file type, corrupted
        # PDF, all of it -- unlike the 'tc_check' event below, which only fires
        # after a file is successfully parsed. Added 2026-09-09: with only that
        # success-only event, there was no way to tell "visitor never touched
        # the widget" apart from "visitor tried and hit an error" -- a landing
        # page can drive visits with zero of either signal moving. Tagged with
        # the same ta_src first-touch cookie as landing_visit so a channel's
        # visits -> attempts -> recognized/complete funnel is fully visible,
        # not just the last two steps.
        track_event("tc_check_attempted", metadata={"source": request.cookies.get("ta_src") or "direct", "source_page": source_page,
                                                    "visitor": request.cookies.get("ta_vid", "")})

    # Product gate re-added 2026-09-10 (see tc_gate.py's own history) --
    # this comment previously said "no product gate," which stopped being
    # true once the email-capture gate below was rebuilt. Email is a
    # non-blocking opt-in ("email me this report + future checks
    # for this address") -- tracked per-browser via an httponly client-id
    # cookie (not IP, since shared offices/NAT would otherwise share one
    # visitor's state) so a returning visitor is identifiable even on a
    # check where they leave the box unchecked. See tc_gate.py.
    cid = request.cookies.get("tc_cid") or str(uuid.uuid4())
    client = get_tc_client(cid)
    submitted_email = (request.form.get("email") or "").strip()

    # Up to 3 files under the same 'file' field: the contract, and
    # optionally its 40-11 Third Party Financing Addendum and/or its 39-11
    # Amendment to Contract, each as a separate PDF (the realistic case --
    # see tc_audit.check_tc_file's docstring for why that's not the same as
    # the FA_-prefix merged-file case).
    uploads = request.files.getlist("file")
    if not uploads or not uploads[0].filename:
        return jsonify({"error": "No file uploaded. Attach a PDF as 'file'."}), 400
    if len(uploads) > 3:
        return jsonify({"error": "Upload at most 3 files: the contract and, if you have them, the 40-11 addendum and/or 39-11 amendment."}), 400
    if any(not f.filename.lower().endswith(".pdf") for f in uploads):
        return jsonify({"error": "Only PDF files are supported."}), 400

    tmp_paths = []
    try:
        for f in uploads:
            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            f.save(tmp.name)
            tmp.close()
            tmp_paths.append(tmp.name)
        try:
            result = check_tc_file(tmp_paths)
        except Exception:
            return jsonify({"error": "Couldn't read that file as a PDF. Make sure it's not corrupted or password-protected."}), 400
    finally:
        for p in tmp_paths:
            try:
                os.remove(p)
            except OSError:
                pass

    if not is_demo:
        record_tc_use(cid)

    # Email-capture gate (see tc_gate.py): the check itself always runs and
    # is tracked in full server-side regardless of what's below -- what's
    # gated is only what's returned to the browser. An anonymous first-time
    # visitor gets a preview (total issue count + the single most severe
    # issue), not the full itemized list, until an email is on file for
    # this browser. Once captured -- this request or a prior one -- every
    # future check for this client id is unlimited. Demo runs are never
    # gated: the sample-file button is meant as a zero-commitment look.
    email_just_captured = False
    if not is_demo and not client["email"] and submitted_email and "@" in submitted_email:
        save_tc_email(cid, submitted_email)
        client["email"] = submitted_email
        email_just_captured = True
        track_event("tc_check_email_captured", submitted_email, {"client_id": cid, "source_page": source_page})

    # client["use_count"] was read before record_tc_use() above, so it's
    # the number of checks this browser ran BEFORE this one. Loosened
    # 2026-10-02 from "preview until email" (2 of 56 web checks gave one):
    # the first TC_FREE_FULL_REPORTS checks show everything, with an
    # optional "email me this report" ask instead of a wall.
    free_report = not client["email"] and client["use_count"] < TC_FREE_FULL_REPORTS
    gate_cleared = is_demo or bool(client["email"]) or free_report

    # Deduped per file -- initials/addendum checks can fire multiple times
    # per file (once per page), and issue_frequency's "% of recognized
    # files" in analytics.py only means what it says if each file counts
    # once per issue type, not once per occurrence. Tracked in full
    # regardless of the gate above -- the gate only changes what's returned
    # to the browser, never what's measured.
    if not is_demo:
        issue_keys = sorted({i["key"] for i in result["issues"] if i.get("key")})
        track_event("tc_check", metadata={
            "recognized": result["recognized"],
            "complete": result["complete"],
            "issue_keys": issue_keys,
            "sender": (client["email"] or "").strip().lower(),
            "gated": not gate_cleared,
            "source_page": source_page,
            "visitor": request.cookies.get("ta_vid", ""),
        })

    payload = dict(result)
    full_issues = payload.get("issues") or []
    payload["issue_count"] = len(full_issues)
    # Exposed ungated same as issue_count above -- the scan-reveal banner on
    # the frontend leads with this number even before an email is on file,
    # same "tell them enough to know it's real" logic as the itemized count.
    payload["blocker_count"] = sum(1 for i in full_issues if i.get("severity") == "blocker")
    if not gate_cleared and full_issues:
        # Reveal scope, not location: how many blockers and which short
        # categories they fall into (deduped, blockers only, in the order
        # they were found), but never the exact section/page/message text
        # -- that specific "where to fix it" detail is the whole reason an
        # email is worth giving. Categories are computed from this file's
        # own real issues, never a fixed/example list.
        categories = []
        for issue in full_issues:
            if issue.get("severity") != "blocker":
                continue
            label = TC_GATE_CATEGORY_LABELS.get(issue.get("key"))
            if label and label not in categories:
                categories.append(label)
        payload["categories"] = categories[:3]
        payload["issues"] = []
        payload["gated"] = True
        # Real, self-updating social proof for the gate -- same MIN_SAMPLE
        # guard as the homepage stats (see index()): never show a stat
        # built from too small a sample to be honest, and never a fixed
        # string that would silently go stale as real usage changes it.
        tc_stats = get_tc_check_summary(days=30)
        if PUBLIC_TC_STATS_ENABLED and tc_stats["recognized"] >= 5:
            proof = f"{tc_stats['recognized']} files checked in the last 30 days — {tc_stats['completion_rate']:g}% came back complete."
            top_issue = tc_stats["issue_frequency"][0] if tc_stats["issue_frequency"] else None
            if top_issue:
                proof += f" {top_issue['label']} on {top_issue['pct_of_recognized']:g}% of them."
            payload["social_proof"] = proof
    else:
        payload["gated"] = False
    # Report-ready view of the same findings (2026-10-07): per-page initials
    # folded into one line, each with its consequence tag -- the exact list
    # the emailed report shows, so the on-screen report and the downloaded
    # PDF match it. Empty while gated, same as "issues".
    from tc_check_email import _display_issues, _consequence_tag, _clean, _files_line
    payload["display_issues"] = sorted(
        ({"severity": i.get("severity"), "tag": _consequence_tag(i), "message": _clean(i.get("message", ""))}
         for i in _display_issues(payload.get("issues") or [])),
        key=lambda i: i["severity"] != "blocker",  # must-fix first, same order as the email/PDF
    )
    payload["files_line"] = _files_line(result) if result.get("recognized") else ""
    if not is_demo and not client["email"]:
        payload["free_reports_left"] = max(TC_FREE_FULL_REPORTS - client["use_count"] - 1, 0)

    resp = jsonify(payload)

    # Fire the opt-in report email on every submission that carries a valid
    # email, not just the first-ever capture -- a returning visitor with the
    # box still checked expects a copy of *this* report too, and repeat
    # sends to the same address are exactly the signal the free-for-now
    # experiment is trying to observe. Reuses the same channel-agnostic
    # formatters as the email-forward path so a web-upload report and a
    # forwarded-file report read identically. Always built from the full,
    # unfiltered result, not the (possibly gated) browser payload above --
    # the emailed report is never gated.
    if not is_demo and submitted_email and "@" in submitted_email:
        check_count = get_tc_check_count_for_sender(submitted_email)

        def _send_report(to_email=submitted_email, res=result, count=check_count):
            try:
                send_html_email(to_email, subject_line(res), format_reply_body(res, count), format_reply_html(res, count))
            except Exception as e:
                print(f"[tc_check] report email failed for {to_email}: {e}")

        threading.Thread(target=_send_report, daemon=True).start()

    resp.set_cookie("tc_cid", cid, max_age=365 * 24 * 3600, httponly=True, samesite="Lax")
    return resp


@app.route("/tc-check/report.pdf", methods=["POST"])
def tc_check_report_pdf():
    """Renders the already-computed TC Check result as a downloadable PDF,
    matching the polish of the branded HTML email (tc_check_email.py)
    instead of the old plain .txt download. Takes only the issue list the
    browser already rendered (see downloadReport() on /tc-check) -- never
    touches or re-reads the original uploaded file, so this can't leak
    anything beyond what a visitor already sees on screen: a client that
    hasn't cleared the email gate only ever has the single preview issue
    to send here in the first place."""
    data = request.get_json(silent=True) or {}
    raw_name = (data.get("filename") or "file.pdf").strip()
    safe_name = "".join(ch for ch in raw_name if ch.isprintable() and ch not in '"\\')[:150] or "file.pdf"
    issues = data.get("issues")
    clean_issues = [
        {
            "severity": i.get("severity") if i.get("severity") in ("blocker", "warning") else "warning",
            "tag": str(i.get("tag") or "")[:40],
            "message": str(i.get("message") or "")[:500],
        }
        for i in (issues if isinstance(issues, list) else [])
        if isinstance(i, dict) and i.get("message")
    ]
    # Same "this is your Nth file" streak line as the emailed report --
    # only when the browser actually knows an email for this visitor (the
    # gate-cleared email, synced into emailOptinInput by unlockGate()).
    email = (data.get("email") or "").strip()
    check_count = get_tc_check_count_for_sender(email) if "@" in email else 0
    raw_prop = data.get("property") if isinstance(data.get("property"), dict) else {}
    prop = {k: str(raw_prop.get(k) or "")[:120] for k in ("address", "city", "county")}
    files_line = str(data.get("files_line") or "")[:80]
    pdf_bytes = generate_tc_report_pdf(safe_name, clean_issues, check_count=check_count,
                                       property=prop, files_line=files_line)
    base_name = safe_name[:-4] if safe_name.lower().endswith(".pdf") else safe_name
    resp = make_response(pdf_bytes)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = f'attachment; filename="{base_name}-tc-check-report.pdf"'
    return resp


@app.route("/v1/tc/check/email/<token>", methods=["POST"])
def tc_check_email_inbound(token):
    """SendGrid Inbound Parse target for TC File Check by email-forward
    (Phase 1 of the document-driven pitch -- see tc_check_email.py). An
    agent/TC forwards a TREC 20-19 (optionally with its 40-11 addendum) to
    a dedicated inbox instead of uploading through /tc-check, and gets the
    same itemized report back by reply.

    No product gate on this path, unlike tc_check() above: the whole point
    of tc_gate.py's email-capture dance is that the web upload doesn't know
    who's asking. A forwarded email already comes with the sender's real
    address, so there's nothing left to gate -- every check gets the full
    itemized reply.

    Inbound Parse has no request signing, so <token> is a shared secret
    living in the URL path itself -- the only thing stopping someone else
    from POSTing a forged 'from' address here and using our SendGrid
    account to relay an auto-reply at a third party (a real risk for any
    endpoint that emails whoever a request claims to be from). Combined
    with the per-sender throttle below. Always returns 200 (SendGrid
    retries non-2xx responses) except on a bad token, where 404 avoids
    even confirming the endpoint exists."""
    if not TC_CHECK_EMAIL_TOKEN or token != TC_CHECK_EMAIL_TOKEN:
        abort(404)

    sender = extract_sender_email(request.form.get("from", ""))
    if not sender:
        return "", 200  # nothing usable to reply to or rate-limit on

    # Spam/spoofed/automated senders: no reply (backscatter) and a separate
    # event type so they stay out of every tc_check metric -- see
    # junk_sender_reason().
    junk_reason = junk_sender_reason(sender, request.form)
    if junk_reason:
        track_event("tc_check_email_rejected", metadata={"reason": junk_reason, "sender": sender})
        return "", 200

    # Keyed by sender email, not IP -- this traffic is relayed through
    # SendGrid, so the request IP is SendGrid's, not the agent's.
    if not check_and_increment(f"tc_check_email:{sender}", limit=10):
        return "", 200

    pdfs = extract_pdf_attachments(request.files, request.form)
    if not pdfs:
        send_html_email(sender, "TC File Check", format_no_pdf_reply(), format_no_pdf_html())
        track_event("tc_check", metadata={"source": "email", "recognized": False, "reason": "no_pdf", "sender": sender})
        return "", 200

    tmp_paths = []
    archive_inputs = []  # (bytes, filename) -- kept only if the sender's brokerage archives forwards
    try:
        for f in pdfs:
            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            f.save(tmp.name)
            tmp.close()
            tmp_paths.append(tmp.name)
            with open(tmp.name, "rb") as fh:
                archive_inputs.append((fh.read(), f.filename or "contract.pdf"))
        try:
            result = check_tc_file(tmp_paths)
        except Exception:
            send_html_email(sender, "TC File Check", format_unreadable_reply(), format_unreadable_html())
            track_event("tc_check", metadata={"source": "email", "recognized": False, "reason": "unreadable", "sender": sender})
            return "", 200
    finally:
        for p in tmp_paths:
            try:
                os.remove(p)
            except OSError:
                pass

    known_agent = find_by_email(sender)
    issue_keys = sorted({i["key"] for i in result["issues"] if i.get("key")})
    track_event("tc_check", metadata={
        "source": "email",
        "recognized": result["recognized"],
        "complete": result["complete"],
        "issue_keys": issue_keys,
        "known_sender": known_agent is not None,
        "sender": sender,
    })

    # Brokerage archive (2026-10-07): a forward from a brokerage's own TC/
    # login email or a roster agent is kept, not just checked -- unless the
    # brokerage switched forward-archiving off. Everyone else: check only.
    archived = None
    brokerage = archive_store.find_brokerage_for_sender(sender)
    if brokerage and brokerage.get("archive_forwards", 1):
        saved = dupes = 0
        for data, name in archive_inputs:
            try:
                rec = archive_store.save_file(brokerage["id"], data, name, "forward", sender, result)
                dupes += 1 if rec.get("duplicate") else 0
                saved += 0 if rec.get("duplicate") else 1
            except Exception as e:
                print(f"[archive] save failed for brokerage {brokerage['id']}: {e}")
        archived = {"brokerage": brokerage.get("name", "your brokerage"), "saved": saved, "duplicates": dupes}
        track_event("archive_saved", None, {"brokerage_id": brokerage["id"], "source": "forward", "files": saved, "duplicates": dupes})

    check_count = get_tc_check_count_for_sender(sender)
    send_html_email(sender, subject_line(result), format_reply_body(result, check_count, archived),
                    format_reply_html(result, check_count, archived))
    return "", 200


@app.route("/v1/tc/compare", methods=["POST"])
def tc_compare():
    """WHAT CHANGED: field-level diff between two uploaded TREC 20-19
    contracts -- e.g. a signed original vs. a later re-filled version of
    the same form. See tc_audit.compare_contracts() for the actual logic
    and its scope (only already-verified fields; closing date and option
    period are explicitly excluded, not mislabeled "unchanged").

    Two EXPLICIT fields, 'original' and 'updated' -- not a file list --
    because two uploads of the same 20-19 template are fingerprint-
    identical; there is no reliable way to infer which is which the way
    /v1/tc/check tells a 40-11 or 39-11 apart from the main contract.

    Linked from /tc-check/compare (added 2026-09-10 -- the logic here
    existed since the file-check work but had no UI in front of it until
    then). Not gated behind email capture like /v1/tc/check is; that's a
    product decision (free vs. requires email, same as the completeness
    check) that hasn't been made for this endpoint yet, not an oversight."""
    client_ip = request.remote_addr or "unknown"
    if not check_and_increment(f"tc_compare:{client_ip}", limit=20):
        return jsonify({"error": "Too many requests. Try again in a bit."}), 429

    original = request.files.get("original")
    updated = request.files.get("updated")
    if not original or not original.filename or not updated or not updated.filename:
        return jsonify({"error": "Upload both files: 'original' and 'updated'."}), 400
    if not original.filename.lower().endswith(".pdf") or not updated.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported."}), 400

    tmp_paths = []
    try:
        orig_tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        original.save(orig_tmp.name)
        orig_tmp.close()
        tmp_paths.append(orig_tmp.name)
        upd_tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        updated.save(upd_tmp.name)
        upd_tmp.close()
        tmp_paths.append(upd_tmp.name)
        try:
            result = compare_contracts(orig_tmp.name, upd_tmp.name)
        except Exception:
            return jsonify({"error": "Couldn't read one of those files as a PDF. Make sure neither is corrupted or password-protected."}), 400
    finally:
        for p in tmp_paths:
            try:
                os.remove(p)
            except OSError:
                pass

    track_event("tc_compare", metadata={"recognized": result.get("recognized")})
    return jsonify(result)


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
--text:#0f1f2f;--text-muted:#5a6b7a;--text-dim:#8a9aa9;--accent:#0b5d52;--accent-light:#16806e;
--accent-dark:#0a3a33;--accent-tint:#E7F3F1;--radius:1.25rem;--radius-sm:0.85rem;}
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
/* Same minimal drop zone as the homepage widget (see index()) */
.drop-zone{border:1.5px dashed #d9dee3;border-radius:16px;background:#fff;padding:2.75rem 1.5rem 2.5rem;
text-align:center;cursor:pointer;transition:border-color 0.18s ease,background 0.18s ease,box-shadow 0.18s ease;}
.drop-zone:hover{border-color:rgba(11,93,82,0.35);background:#fbfdfc;}
.drop-zone.drag{border-style:solid;border-color:var(--accent);background:var(--accent-tint);box-shadow:0 0 0 4px rgba(11,93,82,0.18);}
.drop-zone.drag *{pointer-events:none;}
.dz-icon{width:56px;height:56px;margin:0 auto 1.1rem;border-radius:14px;background:#f5c242;display:flex;align-items:center;justify-content:center;color:#0a3f3a;transition:background 0.18s ease,color 0.18s ease;}
.drop-zone:hover .dz-icon,.drop-zone.drag .dz-icon{background:#0b5d52;color:#f5c242;}
.drop-zone .dz-title{font-weight:600;font-size:1rem;color:var(--text);margin-bottom:0.35rem;}
.drop-zone .dz-sub{color:var(--text-muted);font-size:0.85rem;}
.drop-zone .dz-sub u{text-decoration-color:rgba(11,93,82,0.35);text-underline-offset:3px;color:var(--accent);}
.drop-zone .dz-touch{display:none;}
@media (hover:none){.drop-zone .dz-mouse{display:none;}.drop-zone .dz-touch{display:inline;}}
.drop-zone .dz-meta{color:var(--text-dim);font-size:0.75rem;margin-top:0.85rem;}
.dz-trust{display:flex;justify-content:center;flex-wrap:wrap;gap:0.4rem 1.25rem;margin-top:0.9rem;font-size:0.8rem;color:var(--text-muted);}
.dz-trust span{display:inline-flex;align-items:center;gap:0.35rem;}
.dz-trust svg{color:var(--accent);flex-shrink:0;}
.demo-check-btn{display:block;width:100%;margin-top:1.1rem;padding:0.8rem 1rem;background:#fff;border:1px solid #d9dee3;border-radius:12px;font:inherit;font-size:0.9rem;font-weight:600;color:var(--text);cursor:pointer;text-align:center;transition:all 0.2s;}
.demo-check-btn:hover{border-color:var(--accent);color:var(--accent);box-shadow:0 4px 14px rgba(11,93,82,0.18);}
.demo-check-btn:disabled{opacity:0.6;cursor:default;}
.demo-banner{font-size:0.78rem;font-weight:700;letter-spacing:0.02em;color:var(--text-dim);text-transform:uppercase;margin-bottom:0.5rem;}
.privacy-note{display:flex;align-items:center;gap:0.45rem;margin-top:0.9rem;font-size:0.8rem;color:var(--text-dim);}
.privacy-note svg{flex-shrink:0;}
.or-divider{display:flex;align-items:center;gap:0.75rem;margin:0.15rem 0;font-size:0.7rem;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:var(--text-dim);}
.or-divider::before,.or-divider::after{content:"";flex:1;height:1px;background:var(--border);}
.email-forward-note{display:flex;align-items:center;gap:0.6rem;font-size:0.85rem;color:var(--text-muted);background:var(--accent-tint);border-radius:var(--radius-sm);padding:0.85rem 1rem;}
.email-forward-note svg{flex-shrink:0;color:var(--text-dim);}
.email-forward-note a{color:var(--accent);font-weight:700;text-decoration:underline;text-underline-offset:2px;}
input[type=file]{display:none;}
.status{margin-top:1.25rem;font-size:0.9rem;color:var(--text-muted);display:none;}
.status.show{display:block;}
.result{margin-top:1.5rem;display:none;}
.result.show{display:block;}
.result-banner{border-radius:var(--radius-sm);padding:1rem 1.25rem;font-weight:700;margin-bottom:1rem;}
.result-banner.complete{background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.25);color:#047857;}
.result-banner.incomplete{background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.2);color:#dc2626;}
.rep-head{background:#0a3f3a;border-radius:12px 12px 0 0;padding:16px 18px;display:flex;justify-content:space-between;align-items:center;gap:12px;}
.rep-head img{height:20px;width:auto;display:block;}
.rep-head .rep-kick{font-size:0.62rem;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#f5c242;}
.rep-head .rep-kick{white-space:nowrap;}
@media(max-width:480px){.rep-head{padding:12px 14px;}.rep-head img{height:16px;}.rep-head .rep-kick{font-size:0.55rem;letter-spacing:0.08em;}}
.rep-bar{height:4px;background:#f5c242;margin-bottom:16px;}
.rep-addr{font-size:1.35rem;font-weight:800;letter-spacing:-0.02em;color:var(--text);line-height:1.25;}
.rep-addr.blank{color:#b91c1c;}
.rep-sub{font-size:0.82rem;color:var(--text-muted);margin:4px 0 14px;}
.rep-counts{margin-top:4px;font-size:0.8rem;font-weight:700;}
.rep-counts .must{color:#dc2626;margin-right:10px;}
.rep-counts .review{color:#b45309;}
.issue-item .issue-body{display:flex;flex-direction:column;gap:3px;}
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
font-size:0.85rem;font-weight:600;display:inline-block;max-width:100%;text-align:center;}
.fixit-cta a:hover{opacity:0.9;}
.gate-box{margin-top:0.5rem;padding:1.25rem 1.4rem;background:#0f1f2f;border-radius:var(--radius);}
.gate-shield{display:inline-flex;align-items:center;gap:0.4rem;color:#6fe0c8;font-weight:700;font-size:0.7rem;letter-spacing:0.06em;text-transform:uppercase;margin-bottom:0.6rem;}
.gate-headline{color:#fff;font-weight:700;font-size:1rem;line-height:1.4;margin:0 0 0.9rem;}
.gate-sub{color:rgba(255,255,255,0.72);font-size:0.85rem;margin:0 0 0.9rem;}
.gate-form{display:flex;gap:0.6rem;flex-wrap:wrap;}
.gate-form input{flex:1;min-width:200px;padding:0.7rem 0.9rem;border:1px solid rgba(255,255,255,0.2);border-radius:var(--radius-sm);font-family:inherit;font-size:0.85rem;background:rgba(255,255,255,0.08);color:#fff;}
.gate-form input::placeholder{color:rgba(255,255,255,0.45);}
.gate-form input:focus{outline:none;border-color:var(--accent);}
.gate-form button{background:var(--accent);color:#fff;border:none;padding:0.7rem 1.4rem;border-radius:var(--radius-sm);font-family:inherit;font-size:0.85rem;font-weight:700;cursor:pointer;white-space:nowrap;}
.gate-form button:hover{opacity:0.9;}
.gate-error{color:#fca5a5;font-size:0.78rem;margin-top:0.5rem;min-height:1em;}
.gate-note{color:rgba(255,255,255,0.5);font-size:0.75rem;margin-top:0.7rem;}
.gate-proof{color:rgba(255,255,255,0.65);font-size:0.78rem;margin-top:0.9rem;padding-top:0.7rem;border-top:1px solid rgba(255,255,255,0.08);}
.gate-bridge{margin-top:0.85rem;font-size:0.82rem;color:var(--text-muted);text-align:center;}
.gate-bridge a{color:var(--accent);font-weight:600;}
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
.email-optin{margin-top:1rem;display:flex;flex-direction:column;gap:0.5rem;}
.email-optin-check{display:flex;align-items:center;gap:0.5rem;font-size:0.85rem;color:var(--text-muted);cursor:pointer;}
.email-optin-check input{width:auto;}
.email-optin-input{padding:0.65rem 0.9rem;border:1px solid rgba(15,31,47,0.16);border-radius:var(--radius-sm);font-family:inherit;font-size:0.85rem;background:#fff;color:var(--text);}
.email-optin-input:focus{outline:none;border-color:var(--accent);}
.email-optin-confirm{font-size:0.78rem;color:#047857;display:none;}
.email-optin-confirm.show{display:block;}
.addendum-toggle{margin-top:0.85rem;font-size:0.82rem;color:var(--text-muted);cursor:pointer;text-decoration:underline;text-underline-offset:2px;width:fit-content;}
.addendum-toggle:hover{color:var(--text);}
.addendum-row{margin-top:0.6rem;display:flex;align-items:center;gap:0.6rem;font-size:0.85rem;}
.addendum-row[hidden]{display:none;}
.addendum-filename{color:var(--text-muted);}
.addendum-clear{background:none;border:none;color:var(--text-dim);font-size:1rem;cursor:pointer;line-height:1;padding:0.15rem 0.4rem;}
.addendum-clear:hover{color:var(--text);}
.next-step-cta{margin-top:1.25rem;padding-top:1.1rem;border-top:1px solid var(--border);}
.next-step-lead{font-size:0.85rem;color:var(--text-muted);line-height:1.6;}
.next-step-lead a{color:var(--accent);font-weight:600;text-decoration:underline;text-underline-offset:2px;}
.broker-cta{margin-top:0.9rem;padding:1rem 1.25rem;background:#0f1f2f;border-radius:var(--radius-sm);display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap;}
.broker-cta p{margin:0;font-size:0.85rem;color:rgba(255,255,255,0.85);font-weight:600;}
.broker-cta-btn{background:var(--accent);color:#fff;padding:0.65rem 1.25rem;border-radius:9999px;font-size:0.85rem;font-weight:700;white-space:nowrap;}
.broker-cta-btn:hover{opacity:0.9;}
</style>
</head>
<body>
<nav class="nav">
<a href="/" class="nav-left">
<img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="height:26px;width:auto;display:block;">
</a>
</nav>
<div class="container">
<h1>TC File Check</h1>
<p class="subtitle">Drop a filled TREC 20-19 PDF. We'll tell you what's missing before title kicks it back.</p>
<div class="card">
<div class="drop-zone" id="dropZone">
<div class="dz-icon"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 13v8"/><path d="m8 17 4-4 4 4"/><path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3"/></svg></div>
<div class="dz-title"><span class="dz-mouse">Drop your contract PDF here</span><span class="dz-touch">Choose your contract PDF</span></div>
<div class="dz-sub"><span class="dz-mouse">or <u>click to browse</u></span><span class="dz-touch"><u>Tap to browse files</u></span></div>
<div class="dz-meta">Fillable TREC 20-19 PDF &middot; not scanned or flattened files</div>
</div>
<input type="file" id="fileInput" accept="application/pdf">
<div class="dz-trust">
<span><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>Not kept unless your brokerage archives it</span>
<span><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>Free, no signup</span>
<span><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/></svg>Results in seconds</span>
</div>
<div class="addendum-toggle" id="addendumToggle">+ Also have the 40-11 Financing Addendum? Add it to check the loan amount &amp; checkboxes match, too.</div>
<div class="addendum-row" id="addendumRow" hidden>
<input type="file" id="addendumInput" accept="application/pdf">
<span class="addendum-filename" id="addendumFileName"></span>
<button type="button" class="addendum-clear" id="addendumClear" title="Remove">&times;</button>
</div>
<button type="button" class="demo-check-btn" id="demoBtn">No PDF handy? Run a sample check &rarr;</button>
<div class="email-optin">
<label class="email-optin-check"><input type="checkbox" id="emailOptinCheckbox" checked> Email me this report + future checks for this address</label>
<input type="email" id="emailOptinInput" class="email-optin-input" placeholder="you@example.com" autocomplete="email">
<div class="email-optin-confirm" id="emailOptinConfirm"></div>
</div>
<div class="privacy-note"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>Your PDF is written to a temporary file, checked, then deleted &mdash; normally within seconds (bulk uploads: when the batch finishes). On the Brokerage plan, contracts forwarded from your brokerage&rsquo;s emails go to its archive (on by default; the brokerage can turn it off).</div>
<div class="status" id="status"></div>
<div class="result" id="result"></div>
</div>
<div class="scope-card">
<h3>What this checks</h3>
<div class="scope-grid">
<ul>
<li>Property address, county &mdash; and the address header on every page</li>
<li>Buyer &amp; Seller legal name</li>
<li>Earnest money, option fee, option period days</li>
<li>Escrow agent, title company</li>
<li>Effective Date, and a closing date that's real and after it</li>
<li>Initials on pages 1&ndash;9: one per buyer and seller, from the right person</li>
<li>Sales price math: 3A + 3B = 3C</li>
<li>&ldquo;Check one box only&rdquo; conflicts (survey, Seller&rsquo;s Disclosure, As Is, possession, owners association)</li>
<li>Seller&rsquo;s Disclosure delivery days and 12B broker contributions filled in</li>
<li>Page 12 receipts match the option fee and earnest money</li>
<li>40-11 addendum loan amount, and Third Party Financing listed and attached</li>
<li>Business terms written into Special Provisions (Paragraph 11)</li>
<li>Notice email addresses that would bounce</li>
</ul>
<ul class="not-checked">
<li>Signatures on pages 10&ndash;11</li>
<li>Scanned or flattened PDFs</li>
</ul>
</div>
<p class="scope-footnote">Every check above is verified directly against TREC's actual 20-19 form fields &mdash; not guessed from field names, which routinely lie about their own position. The 40-11 can be its own separate PDF &mdash; it doesn't need to be merged into the contract file.</p>
<p class="scope-footnote">Comparing an original against a later version? <a href="/tc-check/compare" style="color:var(--accent);font-weight:700;text-decoration:underline;text-underline-offset:2px;">See exactly what changed &rarr;</a></p>
<p class="scope-footnote">Auditing a whole closed-file archive? <a href="/tc-check/bulk" style="color:var(--accent);font-weight:700;text-decoration:underline;text-underline-offset:2px;">Bulk-check up to 20 files free (200 with a Brokerage join code) &rarr;</a></p>
<p class="scope-footnote">Want checklists and TREC form references instead? <a href="/tc-hub" style="color:var(--accent);font-weight:700;text-decoration:underline;text-underline-offset:2px;">Visit the free TC Hub &rarr;</a></p>
</div>
</div>
<script>
const dropZone = document.getElementById('dropZone');
const fileInput = document.getElementById('fileInput');
const statusEl = document.getElementById('status');
const resultEl = document.getElementById('result');
const addendumToggle = document.getElementById('addendumToggle');
const addendumRow = document.getElementById('addendumRow');
const addendumInput = document.getElementById('addendumInput');
const addendumFileName = document.getElementById('addendumFileName');
const addendumClear = document.getElementById('addendumClear');
const emailOptinCheckbox = document.getElementById('emailOptinCheckbox');
const emailOptinInput = document.getElementById('emailOptinInput');
const emailOptinConfirm = document.getElementById('emailOptinConfirm');
const demoBtn = document.getElementById('demoBtn');

try { const savedEmail = localStorage.getItem('tc_email_hint'); if (savedEmail) emailOptinInput.value = savedEmail; } catch (e) {}

function optinEmail() {
  const v = emailOptinInput.value.trim();
  if (!emailOptinCheckbox.checked || !v) return '';
  try { localStorage.setItem('tc_email_hint', v); } catch (e) {}
  return v;
}

dropZone.addEventListener('click', () => fileInput.click());
dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('drag');
  if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0], optinEmail());
});
fileInput.addEventListener('change', () => {
  if (fileInput.files.length) uploadFile(fileInput.files[0], optinEmail());
});

if (demoBtn) {
  demoBtn.dataset.label = demoBtn.textContent;
  demoBtn.addEventListener('click', () => {
    demoBtn.disabled = true;
    demoBtn.textContent = 'Checking the sample\u2026';
    fetch('/static/sample_trec_20-19.pdf?v=2026-10-09')
      .then(r => r.blob())
      .then(blob => {
        uploadFile(new File([blob], 'sample_trec_20-19.pdf', {type: 'application/pdf'}), '', true);
      })
      .catch(() => demoDone(false));
  });
  // Arrived here via the homepage's "+N more -- see the full checklist"
  // link on a demo preview: replay the same sample check immediately
  // instead of landing on an empty form the visitor has to re-trigger by
  // hand.
  if (new URLSearchParams(location.search).get('demo') === '1') demoBtn.click();
}

// Same idea as the demo replay above, but for a real file: the homepage
// stashed it as a data URL in sessionStorage (see stashFileForCarry()
// there) right before linking here, since a real upload can't otherwise
// survive a normal navigation -- it's never sent to our servers a second
// time on its own. Read once and clear immediately so a stale stash can
// never replay on a later, unrelated visit to this URL.
if (new URLSearchParams(location.search).get('carry') === '1') {
  let carried = null;
  try {
    const raw = sessionStorage.getItem('tc_carry_file');
    if (raw) carried = JSON.parse(raw);
  } catch (e) {}
  try { sessionStorage.removeItem('tc_carry_file'); } catch (e) {}
  if (carried && carried.dataUrl) {
    fetch(carried.dataUrl)
      .then(r => r.blob())
      .then(blob => {
        const file = new File([blob], carried.name || 'file.pdf', { type: carried.type || 'application/pdf' });
        uploadFile(file, optinEmail(), false);
      })
      .catch(() => {});
  }
}

addendumToggle.addEventListener('click', () => {
  addendumToggle.hidden = true;
  addendumRow.hidden = false;
  addendumInput.click();
});
addendumInput.addEventListener('change', () => {
  if (addendumInput.files.length) {
    pendingAddendumFile = addendumInput.files[0];
    addendumFileName.textContent = pendingAddendumFile.name;
  }
});
addendumClear.addEventListener('click', () => {
  pendingAddendumFile = null;
  addendumInput.value = '';
  addendumFileName.textContent = '';
  addendumRow.hidden = true;
  addendumToggle.hidden = false;
});

// Purely a perceived-progress readout for a request that's actually one
// round trip -- the backend doesn't stream distinct stages back. Labeled
// generically (not "AI analyzing..." theater) and never blocks: whichever
// line is showing when the real response lands, that's when it finishes.
const STATUS_STEPS = ['Scanning TREC 20-19...', 'Checking mandatory fields...', 'Checking initials & consistency...'];
let statusTimers = [];
let pendingFile = null;

// The result renders at the bottom of the card, often below the fold --
// without this the sample button looked like it did nothing (it reset its
// label instantly and the report appeared off-screen).
function demoDone(ok) {
  if (!demoBtn) return;
  demoBtn.textContent = ok ? '\u2713 Done \u2014 results below' : 'Couldn\u2019t load the sample. Try again \u2192';
  setTimeout(() => { demoBtn.textContent = demoBtn.dataset.label; demoBtn.disabled = false; }, ok ? 3500 : 2500);
}
function revealResult() {
  const r = resultEl.getBoundingClientRect();
  if (r.top < 20 || r.top > window.innerHeight * 0.6) resultEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
}
let pendingAddendumFile = null;

function uploadFile(file, email, isDemo) {
  pendingFile = file;
  resultEl.classList.remove('show');
  emailOptinConfirm.classList.remove('show');
  statusTimers.forEach(clearTimeout);
  statusTimers = STATUS_STEPS.map((label, i) =>
    setTimeout(() => { statusEl.textContent = label; }, i * 450)
  );
  statusEl.textContent = STATUS_STEPS[0];
  statusEl.classList.add('show');

  const formData = new FormData();
  formData.append('file', file);
  formData.append('source_page', 'tc_check_page');
  // A real addendum picked for a prior real upload should never ride along
  // on a demo run -- the sample file is the whole check, on its own.
  if (pendingAddendumFile && !isDemo) formData.append('file', pendingAddendumFile);
  if (email) formData.append('email', email);
  if (isDemo) formData.append('is_demo', '1');

  fetch('/v1/tc/check', { method: 'POST', body: formData })
    .then(r => r.json())
    .then(data => {
      statusTimers.forEach(clearTimeout);
      statusEl.classList.remove('show');
      if (data.error) {
        renderError(data.error);
        if (isDemo) demoDone(false);
        revealResult();
        return;
      }
      renderResult(data, file, isDemo);
      if (isDemo) demoDone(true);
      revealResult();
      if (email) {
        emailOptinConfirm.textContent = 'Sent to ' + email;
        emailOptinConfirm.classList.add('show');
      }
    })
    .catch(() => {
      statusTimers.forEach(clearTimeout);
      statusEl.classList.remove('show');
      renderError('Something went wrong checking that file. Try again.');
      if (isDemo) demoDone(false);
      revealResult();
    });
}

function buildMetaBar(data, file) {
  let html = '<div class="meta-bar">';
  html += '<span><strong>' + escapeHtml(file.name) + '</strong></span>';
  html += '<span>' + formatBytes(file.size) + '</span>';
  if (typeof data.page_count === 'number') html += '<span>' + data.page_count + ' page' + (data.page_count === 1 ? '' : 's') + '</span>';
  html += '<span>' + (data.recognized ? 'TREC 20-19 AcroForm detected' : 'Not recognized as a TREC 20-19') + '</span>';
  if (data.has_addendum) html += '<span>40-11 addendum attached' + (pendingAddendumFile ? ' (' + escapeHtml(pendingAddendumFile.name) + ')' : '') + '</span>';
  html += '</div>';
  return html;
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

// Buckets an issue key into which upsell pitch it supports -- each pitch
// below is a claim actually verified against this codebase (send-blocking
// in pdf_validator.py / api_send_email, and the shared loan_amount value
// financing_addendum.py + pdf_filler.py both fill from), not a generic
// "try our tool" line. Deliberately no bucket for initials_buyer/seller:
// TxtAnOffer's own generated contracts leave initials for the agent to add
// after generation too (same as any tool), so there's no honest claim that
// it prevents that specific gap -- those reports fall through to the
// generic default pitch instead of a fabricated one.
const ADDENDUM_MISMATCH_KEYS = new Set(['loan_amount_mismatch', 'addendum_checkbox_mismatch']);
const BLANK_FIELD_KEYS = new Set(['address', 'city', 'county', 'buyer_name', 'seller_name', 'escrow_agent_name', 'earnest_money_amount', 'option_fee_amount', 'title_company', 'effective_date']);

function buildUpsellCta(issues) {
  let addendumCount = 0, blankCount = 0;
  for (const issue of issues) {
    if (ADDENDUM_MISMATCH_KEYS.has(issue.key)) addendumCount++;
    else if (BLANK_FIELD_KEYS.has(issue.key)) blankCount++;
  }
  if (addendumCount > 0) {
    return '<div class="fixit-cta"><p>This file&rsquo;s financing addendum doesn&rsquo;t agree with the contract itself &mdash; the kind of mismatch that only happens when a loan amount gets retyped by hand in two places. TxtAnOffer fills it once and uses it everywhere, so the 40-11 and the contract can never disagree.</p><a href="/signup">Draft your next offer free &mdash; 3 offers, no card &rarr;</a></div>';
  }
  if (blankCount >= 2) {
    return '<div class="fixit-cta"><p>Tired of chasing agents to fill in blank fields? TxtAnOffer&rsquo;s review screen physically blocks emailing the listing agent until every required TREC field is filled in.</p><a href="/signup">Draft your next offer free &mdash; 3 offers, no card &rarr;</a></div>';
  }
  return '<div class="fixit-cta"><p>Every gap above happened because this file was filled out by hand. TxtAnOffer drafts the 20-19 by text message, so these fields are never blank to begin with.</p><a href="/signup">Draft your next offer free &mdash; 3 offers, no card &rarr;</a></div>';
}

// Shown once a report has actually been delivered (email on file, or a
// clean/complete result) -- the point where the old flow just went dead.
// Two guided next steps instead of a static stop: run another file right
// here, or route the next one by email. The Brokerage pitch that used to
// sit here was removed 2026-10-02 so buildUpsellCta()'s free-offer signup
// is the one sales ask after a report.
function buildNextStepCta() {
  return '<div class="next-step-cta">' +
    '<p class="next-step-lead">Want to test another file? <a href="#" onclick="resetForm();return false;">Check another one &rarr;</a></p>' +
  '</div>';
}

// Optional email ask under a free full report -- never blocks anything.
// Reuses unlockGate(), which re-runs the check with the email attached so
// the server both saves it and sends the full report to the inbox.
function buildEmailReportAsk(left) {
  const leftText = left > 0
    ? left + ' more free full report' + (left === 1 ? '' : 's') + ' on this browser, then we ask for an email.'
    : 'That was your last free full report on this browser -- add an email to keep getting them.';
  return '<div class="gate-box">' +
    '<p class="gate-headline">Want this report in your inbox?</p>' +
    '<div class="gate-form"><input type="email" id="gateEmailInput" placeholder="work email" autocomplete="email" onkeydown="if(event.key===\\'Enter\\')unlockGate()"><button type="button" onclick="unlockGate()">Email me this report</button></div>' +
    '<div class="gate-error" id="gateError"></div>' +
    '<div class="gate-note">' + leftText + ' Your file is deleted after the check.</div>' +
  '</div>';
}

function resetForm() {
  resultEl.classList.remove('show');
  resultEl.innerHTML = '';
  pendingFile = null;
  pendingAddendumFile = null;
  fileInput.value = '';
  if (addendumInput) addendumInput.value = '';
  dropZone.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function buildReportHead(data) {
  if (!data.recognized) return '';
  const p = data.property || {};
  let html = '<div class="rep-head"><img src="/static/logo-wordmark-white.png?v=1" alt="txtanoffer"><span class="rep-kick">TC File Check report</span></div><div class="rep-bar"></div>';
  html += p.address
    ? '<div class="rep-addr">' + escapeHtml(p.address) + '</div>'
    : '<div class="rep-addr blank">Property address not filled in</div>';
  const bits = [];
  if (p.address) { if (p.city) bits.push(escapeHtml(p.city)); if (p.county) bits.push(escapeHtml(p.county) + ' County'); }
  else bits.push('Section 2A is blank');
  if (data.files_line) bits.push(escapeHtml(data.files_line));
  html += '<div class="rep-sub">' + bits.join(' &middot; ') + '</div>';
  return html;
}

function renderResult(data, file, isDemo) {
  const issues = (data.display_issues && data.display_issues.length) ? data.display_issues : (data.issues || []);
  const totalIssues = typeof data.issue_count === 'number' ? data.issue_count : issues.length;
  let html = isDemo ? '<div class="demo-banner">Demo result &mdash; a sample contract with planted mistakes, not your file.</div>' : '';
  html += buildReportHead(data);
  html += buildMetaBar(data, file);

  if (data.complete) {
    html += '<div class="result-banner complete">All checked fields are filled in.</div>';
  } else {
    // Lead with the blocking count -- the concrete "title will reject this"
    // number -- when there is one; fall back to a plain issue count for a
    // file whose only findings are non-blocking warnings.
    const blockerCount = typeof data.blocker_count === 'number' ? data.blocker_count : 0;
    const bannerText = blockerCount > 0
      ? blockerCount + ' title-blocking error' + (blockerCount === 1 ? '' : 's') + ' found'
      : totalIssues + ' issue' + (totalIssues === 1 ? '' : 's') + ' found';
    let counts = '';
    if (data.display_issues && data.display_issues.length) {
      const nb = data.display_issues.filter(i => i.severity === 'blocker').length;
      const nw = data.display_issues.length - nb;
      counts = '<div class="rep-counts">' + (nb ? '<span class="must">' + nb + ' must fix</span>' : '') + (nw ? '<span class="review">' + nw + ' to review</span>' : '') + '</div>';
      html += '<div class="result-banner incomplete">' + (nb ? 'Not ready &mdash; fix before this goes to title' : nw + ' item' + (nw === 1 ? '' : 's') + ' to review') + counts + '</div>';
    } else {
      html += '<div class="result-banner incomplete">' + bannerText + '</div>';
    }
  }
  if (data.looks_like_blank_draft) {
    html += '<div class="fixit-cta"><p>This looks like an essentially blank draft &mdash; more gaps than a quick fix. It may be faster to generate a clean one from scratch.</p><a href="/demo">Generate a clean offer &rarr;</a></div>';
  }
  if (issues.length) {
    html += '<ul class="issue-list">';
    for (const issue of issues) {
      html += '<li class="issue-item"><span class="issue-body"><span><span class="issue-tag ' + issue.severity + '">' + escapeHtml(issue.tag || issue.severity) + '</span></span><span>' + escapeHtml(issue.message) + '</span></span></li>';
    }
    html += '</ul>';
  }
  if (data.gated) {
    // Reveals scope (count + which categories), never location -- exact
    // pages/lines are the specific thing an email buys. Categories come
    // from this file's own real blockers (see /v1/tc/check), never a
    // fixed example list, so the teaser can't say something untrue about
    // a file it hasn't actually found those problems on.
    const blockerCount = typeof data.blocker_count === 'number' ? data.blocker_count : totalIssues;
    const cats = Array.isArray(data.categories) ? data.categories : [];
    let gateHeadline;
    if (blockerCount > 0 && cats.length) {
      const catText = cats.length > 1
        ? cats.slice(0, -1).join(', ') + ' and ' + cats[cats.length - 1]
        : cats[0];
      gateHeadline = blockerCount + ' blocker' + (blockerCount === 1 ? '' : 's') + ' found that will bounce at Title — including ' + catText + '.';
    } else if (blockerCount > 0) {
      gateHeadline = blockerCount + ' title-blocking error' + (blockerCount === 1 ? '' : 's') + ' found.';
    } else {
      gateHeadline = 'Where should we send this secure audit report so you can review it before your deadline?';
    }
    html += '<div class="gate-box">';
    html += '<div class="gate-shield">&#128737; Secure Audit Report</div>';
    html += '<p class="gate-headline">' + gateHeadline + '</p>';
    html += '<p class="gate-sub">You&rsquo;ve used your free full reports on this browser. Enter your email to see the exact pages and lines &mdash; your file is deleted after the check.</p>';
    html += '<div class="gate-form"><input type="email" id="gateEmailInput" placeholder="work email" autocomplete="email" onkeydown="if(event.key===\\'Enter\\')unlockGate()"><button type="button" onclick="unlockGate()">See my bounce report</button></div>';
    html += '<div class="gate-error" id="gateError"></div>';
    if (data.social_proof) {
      html += '<div class="gate-proof">' + escapeHtml(data.social_proof) + '</div>';
    }
    html += '<div class="gate-note">Free, no card, no signup &mdash; unsubscribe anytime.</div>';
    html += '</div>';
    html += '<div class="gate-bridge">Leading a team? <a href="/pricing#brokerage">See how Brokerage auto-checks every agent\\'s offer before it reaches you &rarr;</a></div>';
  } else {
    if (issues.length) {
      html += '<button class="copy-btn" onclick="copyChecklist()">Copy checklist</button>';
      html += '<button class="download-btn" onclick="downloadReport(this)">Download report</button>';
      html += buildUpsellCta(issues);
    }
    if (!isDemo && typeof data.free_reports_left === 'number') html += buildEmailReportAsk(data.free_reports_left);
    if (!isDemo) html += buildNextStepCta();
  }
  resultEl.innerHTML = html;
  resultEl.classList.add('show');
  resultEl.dataset.issues = JSON.stringify(issues);
  resultEl.dataset.filename = file.name;
  resultEl.dataset.property = JSON.stringify(data.property || {});
  resultEl.dataset.filesLine = data.files_line || '';
}

function unlockGate() {
  const input = document.getElementById('gateEmailInput');
  const errorEl = document.getElementById('gateError');
  const email = (input && input.value || '').trim();
  if (!email || email.indexOf('@') === -1) {
    if (errorEl) errorEl.textContent = 'Enter a valid email to see the full report.';
    return;
  }
  if (errorEl) errorEl.textContent = '';
  try { localStorage.setItem('tc_email_hint', email); } catch (e) {}
  if (emailOptinInput) emailOptinInput.value = email;
  if (pendingFile) uploadFile(pendingFile, email, false);
}

function checklistText() {
  const issues = JSON.parse(resultEl.dataset.issues || '[]');
  return issues.map(i => '- [' + i.severity.toUpperCase() + '] ' + i.message).join('\\n');
}

function copyChecklist() {
  navigator.clipboard.writeText(checklistText());
}

function downloadReport(btn) {
  const filename = resultEl.dataset.filename || 'file.pdf';
  const issues = JSON.parse(resultEl.dataset.issues || '[]');
  const property = JSON.parse(resultEl.dataset.property || '{}');
  const files_line = resultEl.dataset.filesLine || '';
  const email = (emailOptinInput && emailOptinInput.value || '').trim();
  const original = btn ? btn.textContent : null;
  if (btn) { btn.disabled = true; btn.textContent = 'Preparing PDF...'; }
  fetch('/tc-check/report.pdf', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename, issues, email, property, files_line })
  })
    .then(r => r.blob())
    .then(blob => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename.replace(/\\.pdf$/i, '') + '-tc-check-report.pdf';
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    })
    .catch(() => { alert('Could not generate the PDF. Try again.'); })
    .finally(() => { if (btn) { btn.disabled = false; btn.textContent = original; } });
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
    track_page_view(resp, "tc_check_page")
    return resp


_BULK_PAGE_STYLE = """
:root{--bg:#F5F5F7;--bg-card:#fff;--border:rgba(15,31,47,0.08);
--text:#0f1f2f;--text-muted:#5a6b7a;--text-dim:#8a9aa9;--accent:#0b5d52;--accent-light:#16806e;
--accent-tint:#E7F3F1;--radius:1.25rem;--radius-sm:0.85rem;}
*{margin:0;padding:0;box-sizing:border-box;}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;-webkit-font-smoothing:antialiased;}
a{color:var(--accent);}
.bulk-nav{max-width:560px;margin:0 auto;padding:1.5rem 2rem 0;}
.bulk-nav-link{display:inline-flex;align-items:center;gap:0.5rem;font-weight:700;font-size:1rem;color:var(--text);text-decoration:none;}
.bulk-logo{width:28px;height:28px;border-radius:22%;object-fit:contain;display:block;}
.container{max-width:560px;margin:0 auto;padding:3rem 2rem;}
h1{font-size:1.75rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.5rem;}
.subtitle{color:var(--text-muted);font-size:0.95rem;margin-bottom:2rem;line-height:1.5;}
.card{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:2rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);}
label{display:block;font-size:0.85rem;font-weight:600;margin-bottom:0.4rem;}
input[type=email],input[type=file],input[type=text]{display:block;width:100%;padding:0.7rem 0.9rem;border:1px solid var(--border);border-radius:var(--radius-sm);font-family:inherit;font-size:0.9rem;margin-bottom:1.25rem;background:#fff;box-sizing:border-box;}
.hint{font-size:0.78rem;color:var(--text-dim);margin-top:-1rem;margin-bottom:1.25rem;}
.submit-btn{background:var(--accent);color:#fff;border:none;padding:0.8rem 1.6rem;border-radius:var(--radius-sm);font-family:inherit;font-size:0.9rem;font-weight:600;cursor:pointer;width:100%;}
.submit-btn:hover{opacity:0.9;}
.error-box{background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.2);color:#dc2626;border-radius:var(--radius-sm);padding:0.9rem 1.1rem;font-size:0.85rem;margin-bottom:1.25rem;}
.stat-banner{border-radius:var(--radius-sm);padding:1rem 1.25rem;font-weight:700;margin-bottom:1.5rem;background:rgba(245,158,11,0.10);color:#b45309;font-size:1.05rem;}
.issue-row{display:flex;justify-content:space-between;gap:1rem;padding:0.6rem 0;border-bottom:1px solid var(--border);font-size:0.88rem;}
.issue-row:last-child{border-bottom:none;}
.issue-count{flex-shrink:0;font-weight:700;color:var(--text-muted);}
table.file-table{width:100%;font-size:0.82rem;border-collapse:collapse;margin-top:0.5rem;}
table.file-table th{text-align:left;color:var(--text-dim);font-weight:600;font-size:0.72rem;text-transform:uppercase;letter-spacing:0.04em;padding-bottom:0.5rem;border-bottom:1px solid var(--border);}
table.file-table td{padding:0.5rem 0;border-bottom:1px solid var(--border);vertical-align:top;}
.badge{font-size:0.68rem;font-weight:700;text-transform:uppercase;letter-spacing:0.03em;padding:0.12rem 0.45rem;border-radius:9999px;}
.badge.clean{background:rgba(16,185,129,0.12);color:#047857;}
.badge.issues{background:rgba(239,68,68,0.1);color:#dc2626;}
.badge.unread{background:rgba(15,31,47,0.08);color:var(--text-dim);}
"""


@app.route("/tc-check/bulk", methods=["GET"])
def tc_check_bulk_page():
    """Self-serve entry point for auditing a whole batch of closed files
    at once instead of one at a time -- see tc_bulk.py for why this needs
    an email (results take a few minutes, so there's nothing to show
    synchronously) and why it's a plain form POST rather than the AJAX/
    drag-drop widget on /tc-check (no perceived-progress trick makes sense
    when the real wait is minutes, not seconds)."""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bulk TC File Check — TxtAnOffer</title>
<meta name="description" content="Upload a zip of closed TREC 20-19 files and get an aggregate report of what's missing across all of them.">
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>{_BULK_PAGE_STYLE}</style>
</head>
<body>
<div class="bulk-nav"><a href="/" class="bulk-nav-link"><img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="height:24px;width:auto;display:block;"></a></div>
<div class="container">
<h1>Bulk TC File Check</h1>
<p class="subtitle">Zip up to {FREE_BULK_LIMIT} closed TREC 20-19 files (contracts only, one per transaction) for a free sample report: how many had at least one issue, and which issues showed up most across the batch. Have a Brokerage join code? Check your whole backlog, up to {MAX_BULK_FILES} files, no extra charge.</p>
<div class="card">
<form method="POST" action="/tc-check/bulk" enctype="multipart/form-data">
<label for="email">Email (required &mdash; we'll send your report here)</label>
<input type="email" id="email" name="email" required placeholder="you@example.com">
<label for="file">Zip file of PDFs</label>
<input type="file" id="file" name="file" accept=".zip" required>
<label for="join_code">Brokerage join code (optional)</label>
<input type="text" id="join_code" name="join_code" placeholder="Leave blank for the free sample">
<p class="hint">Each check runs alone (no addendum cross-check inside a batch) &mdash; for the full field-by-field addendum comparison on one file at a time, use <a href="/tc-check">the single-file checker</a>.</p>
<button type="submit" class="submit-btn">Check my files</button>
</form>
</div>
</div>
</body>
</html>"""
    return make_response(html)


@app.route("/tc-check/bulk", methods=["POST"])
def tc_check_bulk_submit():
    client_ip = request.remote_addr or "unknown"
    email = (request.form.get("email") or "").strip()

    def _error_page(message):
        html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bulk TC File Check — TxtAnOffer</title>
<style>{_BULK_PAGE_STYLE}</style></head><body>
<div class="bulk-nav"><a href="/" class="bulk-nav-link"><img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="height:24px;width:auto;display:block;"></a></div>
<div class="container"><h1>Bulk TC File Check</h1>
<div class="card"><div class="error-box">{escape(message)}</div>
<a href="/tc-check/bulk">&larr; Try again</a></div></div></body></html>"""
        return make_response(html, 400)

    if not email or "@" not in email:
        return _error_page("Enter a valid email address -- that's where your report will be sent.")

    # Two separate abuse guards, day-long windows since a real batch takes
    # a few minutes of background work per submission (unlike /tc-check's
    # per-request throttle): per-IP catches one abusive source hammering
    # this from different email addresses, per-email catches one address
    # resubmitting from different IPs.
    if not check_and_increment(f"tc_bulk_ip:{client_ip}", limit=3, window_seconds=86400):
        return _error_page("Too many bulk batches from this connection today. Try again tomorrow.")
    if not check_and_increment(f"tc_bulk_email:{email.lower()}", limit=3, window_seconds=86400):
        return _error_page("Too many bulk batches from this email today. Try again tomorrow.")

    # An optional join_code raises the file cap from FREE_BULK_LIMIT to
    # MAX_BULK_FILES -- see tc_bulk.py's module docstring for why: the
    # free tier is a real, fully-detailed sample, not a substitute for the
    # paid Brokerage Dashboard's job of checking a whole backlog for free.
    join_code = (request.form.get("join_code") or "").strip()
    brokerage = get_brokerage_by_code(join_code) if join_code else None
    if join_code and not brokerage:
        return _error_page("That join code wasn't recognized. Double-check it, or leave it blank for the free sample.")
    tier = "brokerage" if brokerage else "free"
    file_limit = MAX_BULK_FILES if brokerage else FREE_BULK_LIMIT
    brokerage_name = brokerage["name"] if brokerage else None

    upload = request.files.get("file")
    if not upload or not upload.filename:
        return _error_page("No zip file uploaded.")
    if not upload.filename.lower().endswith(".zip"):
        return _error_page("Only .zip files are supported.")

    batch_id = uuid.uuid4().hex
    tmp_dir = tempfile.mkdtemp(prefix=f"tc_bulk_{batch_id}_")
    zip_path = os.path.join(tmp_dir, "upload.zip")
    upload.save(zip_path)

    try:
        files = extract_pdfs_from_zip(zip_path, tmp_dir, max_files=file_limit)
    except BulkUploadError as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return _error_page(str(e))
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass

    create_batch(batch_id, email, len(files))
    track_event("tc_check_bulk_submitted", metadata={"batch_id": batch_id, "file_count": len(files), "tier": tier})
    threading.Thread(
        target=process_batch, args=(batch_id, email, files, tmp_dir),
        kwargs={"tier": tier, "brokerage_name": brokerage_name}, daemon=True,
    ).start()

    return redirect(f"/tc-check/bulk/{batch_id}")


@app.route("/tc-check/bulk/<batch_id>")
def tc_check_bulk_results(batch_id):
    """No auth beyond the batch_id itself -- same unguessable-token
    pattern as /thread/<filename> and a brokerage join_code. Processing
    isn't done yet on most first loads (see process_batch's background
    thread), so this meta-refreshes every 10s until status flips to done;
    no JS polling since a self-serve prospect landing here for the first
    time shouldn't need working JS to eventually see their results."""
    batch = get_batch(batch_id)
    if not batch:
        return make_response("Batch not found.", 404)

    if batch["status"] == "processing":
        html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="10">
<title>Bulk TC File Check — TxtAnOffer</title>
<style>{_BULK_PAGE_STYLE}</style></head><body>
<div class="bulk-nav"><a href="/" class="bulk-nav-link"><img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="height:24px;width:auto;display:block;"></a></div>
<div class="container"><h1>Still checking&hellip;</h1>
<p class="subtitle">Checking {batch['file_count']} file(s). This page refreshes automatically -- we'll also email the report to {escape(batch['email'])} the moment it's ready.</p>
</div></body></html>"""
        return make_response(html)

    if batch["status"] == "error" or not batch["result"]:
        html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bulk TC File Check — TxtAnOffer</title>
<style>{_BULK_PAGE_STYLE}</style></head><body>
<div class="bulk-nav"><a href="/" class="bulk-nav-link"><img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="height:24px;width:auto;display:block;"></a></div>
<div class="container"><h1>Something went wrong</h1>
<div class="card"><div class="error-box">{escape(batch.get('error') or 'This batch could not be processed.')}</div>
<a href="/tc-check/bulk">&larr; Try again</a></div></div></body></html>"""
        return make_response(html)

    result = batch["result"]
    issue_rows = "".join(
        f'<div class="issue-row"><span>{escape(item["message"])}</span><span class="issue-count">{item["count"]}x</span></div>'
        for item in result["top_issues"]
    ) or '<p class="hint" style="margin:0;">No recurring issues found.</p>'

    def _badge(f):
        if f["unreadable"] or not f["recognized"]:
            return '<span class="badge unread">Unreadable</span>'
        return '<span class="badge clean">Clean</span>' if f["complete"] else f'<span class="badge issues">{f["issue_count"]} issue(s)</span>'

    file_rows = "".join(
        f'<tr><td>{escape(f["filename"])}</td><td>{_badge(f)}</td></tr>'
        for f in result["per_file"]
    )

    if result.get("tier") == "brokerage":
        footer_note = f"Checked under your {escape(result.get('brokerage_name') or 'Brokerage')} account &mdash; no file cap."
    else:
        footer_note = (
            f"This was your free {FREE_BULK_LIMIT}-file sample. "
            'Want your whole backlog checked, with no cap? <a href="/pricing#brokerage">See the Brokerage Dashboard &rarr;</a>'
        )

    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bulk TC File Check results — TxtAnOffer</title>
<style>{_BULK_PAGE_STYLE}</style></head><body>
<div class="bulk-nav"><a href="/" class="bulk-nav-link"><img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="height:24px;width:auto;display:block;"></a></div>
<div class="container">
<h1>Batch results</h1>
<p class="subtitle">{result['total_files']} file(s) checked, {result['recognized_count']} recognized as a TREC 20-19.</p>
<div class="stat-banner">{result['with_issues_count']} of {result['recognized_count']} files had at least one issue</div>
<div class="card">
<p class="hint" style="margin:0 0 0.75rem;text-transform:uppercase;font-weight:700;letter-spacing:0.04em;">Most common issues in this batch</p>
{issue_rows}
</div>
<div class="card" style="margin-top:1.5rem;">
<table class="file-table"><thead><tr><th>File</th><th>Result</th></tr></thead><tbody>
{file_rows}
</tbody></table>
</div>
<p class="hint" style="margin-top:1.5rem;">{footer_note}</p>
</div></body></html>"""
    return make_response(html)


@app.route("/tc-check/compare")
def tc_check_compare_page():
    """UI for the 'WHAT CHANGED' field-level diff -- the compare_contracts()
    logic and its /v1/tc/compare endpoint have existed since the file-check
    feature work, but nothing ever linked to them. Synchronous like the
    single-file /tc-check widget (reading two files' field values is fast,
    nothing like the minutes a bulk zip takes), so this reuses that same
    instant drag-drop-and-fetch pattern rather than /tc-check/bulk's
    email-and-wait one. Two explicit named slots (original/updated), not a
    drop-anything zone, since the backend can't infer which upload is which
    -- see compare_contracts()'s own docstring."""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Compare Contract Versions — TxtAnOffer</title>
<meta name="description" content="Upload the original and an updated TREC 20-19 side by side and see exactly which fields changed, which stayed the same, and which are still blank in both.">
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>{_BULK_PAGE_STYLE}
.slot{{margin-bottom:1.25rem;}}
.slot input[type=file]{{margin-bottom:0.3rem;}}
.slot .filename{{font-size:0.78rem;color:var(--text-dim);}}
.status-pill{{font-size:0.68rem;font-weight:700;text-transform:uppercase;letter-spacing:0.03em;padding:0.12rem 0.5rem;border-radius:9999px;white-space:nowrap;}}
.status-pill.changed{{background:rgba(245,158,11,0.12);color:#b45309;}}
.status-pill.unchanged{{background:rgba(16,185,129,0.12);color:#047857;}}
.status-pill.missing{{background:rgba(15,31,47,0.08);color:var(--text-dim);}}
.diff-value{{font-size:0.8rem;color:var(--text-muted);}}
.diff-value b{{color:var(--text);font-weight:600;}}
</style>
</head>
<body>
<div class="bulk-nav"><a href="/" class="bulk-nav-link"><img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="height:24px;width:auto;display:block;"></a></div>
<div class="container">
<h1>Compare Contract Versions</h1>
<p class="subtitle">Upload the original TREC 20-19 and a later version of the same file &mdash; after an amendment, a re-send, or just to double-check nothing drifted. See exactly what changed, what's unchanged, and what's still blank in both. Free, no login, nothing stored.</p>
<div class="card">
<div class="slot">
<label for="original">Original file</label>
<input type="file" id="original" accept="application/pdf">
<div class="filename" id="originalName">No file chosen</div>
</div>
<div class="slot">
<label for="updated">Updated file</label>
<input type="file" id="updated" accept="application/pdf">
<div class="filename" id="updatedName">No file chosen</div>
</div>
<button type="button" class="submit-btn" id="compareBtn">Compare</button>
<p class="hint" id="status" style="display:none;margin-top:1rem;margin-bottom:0;"></p>
</div>
<div id="result"></div>
</div>
<script>
var originalInput = document.getElementById('original'),
    updatedInput = document.getElementById('updated'),
    originalName = document.getElementById('originalName'),
    updatedName = document.getElementById('updatedName'),
    compareBtn = document.getElementById('compareBtn'),
    statusEl = document.getElementById('status'),
    resultEl = document.getElementById('result');

originalInput.addEventListener('change', function(){{
  originalName.textContent = originalInput.files.length ? originalInput.files[0].name : 'No file chosen';
}});
updatedInput.addEventListener('change', function(){{
  updatedName.textContent = updatedInput.files.length ? updatedInput.files[0].name : 'No file chosen';
}});

function escapeHtml(s){{
  var div = document.createElement('div');
  div.textContent = s;
  return div.innerHTML;
}}

function renderError(msg){{
  resultEl.innerHTML = '<div class="card" style="margin-top:1.5rem;"><div class="error-box">' + escapeHtml(msg) + '</div></div>';
}}

function renderResult(data){{
  if (data.error) {{ renderError(data.error); return; }}
  if (!data.recognized) {{ renderError(data.message || "Didn't recognize one of those as a TREC 20-19."); return; }}
  var changed = data.changes.filter(function(c){{ return c.status === 'changed'; }}).length;
  var html = '<div class="card" style="margin-top:1.5rem;">';
  html += '<div class="stat-banner">' + changed + ' field' + (changed === 1 ? '' : 's') + ' changed between versions</div>';
  html += '<table class="file-table"><thead><tr><th>Field</th><th>Status</th><th>Detail</th></tr></thead><tbody>';
  data.changes.forEach(function(c){{
    var pill, detail;
    if (c.status === 'changed') {{
      pill = '<span class="status-pill changed">Changed</span>';
      detail = '<span class="diff-value">' + escapeHtml(c.from) + ' &rarr; <b>' + escapeHtml(c.to) + '</b></span>';
    }} else if (c.status === 'unchanged') {{
      pill = '<span class="status-pill unchanged">Unchanged</span>';
      detail = '<span class="diff-value">&mdash;</span>';
    }} else {{
      pill = '<span class="status-pill missing">Blank in both</span>';
      detail = '<span class="diff-value">&mdash;</span>';
    }}
    html += '<tr><td>' + escapeHtml(c.field) + '</td><td>' + pill + '</td><td>' + detail + '</td></tr>';
  }});
  html += '</tbody></table>';
  if (data.not_compared && data.not_compared.length) {{
    html += '<p class="hint" style="margin-top:1rem;">Not compared: ' + data.not_compared.map(escapeHtml).join(', ') + '. ' + escapeHtml(data.not_compared_reason || '') + '</p>';
  }}
  html += '</div>';
  resultEl.innerHTML = html;
}}

compareBtn.addEventListener('click', function(){{
  if (!originalInput.files.length || !updatedInput.files.length) {{
    renderError('Choose both an original and an updated file first.');
    return;
  }}
  resultEl.innerHTML = '';
  statusEl.style.display = 'block';
  statusEl.textContent = 'Comparing...';
  compareBtn.disabled = true;

  var formData = new FormData();
  formData.append('original', originalInput.files[0]);
  formData.append('updated', updatedInput.files[0]);

  fetch('/v1/tc/compare', {{ method: 'POST', body: formData }})
    .then(function(r){{ return r.json(); }})
    .then(function(data){{
      statusEl.style.display = 'none';
      compareBtn.disabled = false;
      renderResult(data);
    }})
    .catch(function(){{
      statusEl.style.display = 'none';
      compareBtn.disabled = false;
      renderError('Something went wrong comparing those files. Try again.');
    }});
}});
</script>
</body>
</html>"""
    return make_response(html)


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
--text:#0f1f2f;--text-muted:#5a6b7a;--text-dim:#8a9aa9;--accent:#0b5d52;--accent-light:#16806e;
--accent-dark:#0a3a33;--accent-tint:#E7F3F1;--radius:1.25rem;--radius-sm:0.85rem;}
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
.pdf-demo{margin-top:1.25rem;padding-top:1.25rem;border-top:1px solid var(--border);text-align:center;}
.pdf-btn{background:linear-gradient(135deg,var(--accent),#0a3a33);color:#fff;border:none;
padding:0.75rem 1.75rem;border-radius:var(--radius-sm);font-family:inherit;font-size:0.85rem;
font-weight:600;cursor:pointer;transition:all 0.2s;}
.pdf-btn:hover{transform:translateY(-2px);box-shadow:0 8px 24px rgba(11,93,82,0.25);}
.pdf-btn:disabled{opacity:0.6;cursor:default;transform:none;box-shadow:none;}
.pdf-result{margin-top:1rem;display:none;}
.pdf-result.show{display:flex;align-items:center;justify-content:center;gap:0.75rem;flex-wrap:wrap;}
.pdf-result a{background:var(--accent-tint);color:var(--accent-dark);border:1px solid rgba(11,93,82,0.2);
padding:0.6rem 1.1rem;border-radius:9999px;font-size:0.85rem;font-weight:600;}
.pdf-hint{font-size:0.78rem;color:var(--text-dim);margin-top:0.6rem;}
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
<img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="height:26px;width:auto;display:block;">
</a>
<div class="nav-links" id="navLinks">
<a href="/#how">How it works</a>
<a href="/pricing">Pricing</a>
<a href="/faq">FAQ</a>
<a href="/login">Log In</a>
</div>
<a href="/tc-check" class="nav-cta">Try TC Check Free</a>
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
<div class="pdf-demo">
<button class="pdf-btn" id="pdf-btn">Generate the real PDF &rarr;</button>
<div class="pdf-result" id="pdf-result"></div>
<div class="pdf-hint">This fills an actual TREC 20-19 &mdash; the same PDF you'd get back by text.</div>
</div>
</div>

<div class="examples">
<h3>Try these (click to load):</h3>
<div class="example-chips">
<span class="chip">725k 3% 21day 123 Main St, Austin, TX</span>
<span class="chip">Offer 650000 3 percent close in 30 days 123 Main St Austin</span>
<span class="chip">500k 5 down 14days 123 Main St Plano</span>
<span class="chip">1.2m 10% 45day Travis 123 Main St</span>
<span class="chip">825k 3% close in 14 123 Main St Houston</span>
<span class="chip">375,000 3% 30days 123 Main St Dallas</span>
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
    warnEl=document.getElementById('warn-msg'),
    pdfBtn=document.getElementById('pdf-btn'),
    pdfResult=document.getElementById('pdf-result');

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
  pdfResult.classList.remove('show');
  pdfResult.innerHTML='';
  pdfBtn.disabled=false;
  pdfBtn.textContent='Generate the real PDF →';
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

pdfBtn.addEventListener('click',function(){
  var text=input.value.trim();
  if(!text)return;
  pdfBtn.disabled=true;
  pdfBtn.textContent='Generating…';
  pdfResult.classList.remove('show');
  fetch('/api/demo',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({offer_text:text})})
  .then(function(r){return r.json();})
  .then(function(d){
    if(d.error){
      pdfBtn.disabled=false;
      pdfBtn.textContent='Generate the real PDF →';
      errEl.textContent=d.error;
      errEl.classList.add('show');
      return;
    }
    pdfBtn.textContent='Generate another →';
    pdfBtn.disabled=false;
    pdfResult.innerHTML='<a href="'+d.pdf_url+'" target="_blank">View the filled TREC 20-19 &rarr;</a>';
    pdfResult.classList.add('show');
  })
  .catch(function(){
    pdfBtn.disabled=false;
    pdfBtn.textContent='Generate the real PDF →';
    errEl.textContent='Something went wrong generating the PDF.';
    errEl.classList.add('show');
  });
});

// Auto-run the classic example on load so the page demos itself immediately
// -- text in, structured fields out, with the real PDF one click away.
input.value='725k 3% 21day 123 Main St, Austin, TX';
btn.click();
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
    if result["success"]:
        record_docusign_sent(pdf_filename, result.get("envelope_id", ""))
    return jsonify(result), 200 if result["success"] else 500


@app.route("/pricing")
def pricing():
    html = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pricing — TxtAnOffer</title>
<meta name="description" content="TC File Check is free for Texas TCs and agents: drop a TREC 20-19 and see what title would kick back. Agent plan $40/mo for unlimited offers by text. Brokerage $349/mo covers your whole roster.">
<meta property="og:title" content="TxtAnOffer Pricing — Free to Check, Simple to Scale">
<meta property="og:description" content="TC File Check is free. Agent plan $40/mo, Brokerage $349/mo for your whole roster.">
<meta property="og:url" content="https://txtanoffer.com/pricing">
<meta property="og:type" content="website">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="TxtAnOffer Pricing — Free to Check, Simple to Scale">
<meta name="twitter:description" content="TC File Check is free. Agent plan $40/mo, Brokerage $349/mo for your whole roster.">
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
  :root {
    --bg: #F5F5F7;
    --bg-card: #fff;
    --border: rgba(15,31,47,0.08);
    --border-hover: rgba(11,93,82,0.35);
    --text: #0f1f2f;
    --text-muted: #5a6b7a;
    --text-dim: #8a9aa9;
    --accent: #0b5d52;
    --accent-light: #16806e;
    --accent-dark: #0a3a33;
    --accent-tint: #E7F3F1;
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
  .nav-cta:hover {transform:scale(1.05);box-shadow:0 0 24px rgba(11,93,82,0.3);}
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
    background:linear-gradient(135deg,var(--accent),#0a3a33);color:#fff;
    border:none;font-family:inherit;font-size:0.9rem;font-weight:600;
    border-radius:var(--radius-sm);cursor:pointer;text-align:center;
    transition:var(--transition);text-decoration:none;
  }
  .cta-btn:hover {transform:translateY(-2px);box-shadow:0 8px 24px rgba(11,93,82,0.25);}
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

  .nav-cta {white-space:nowrap;}
  @media(max-width:480px) { .nav-cta {display:none;} }
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
    <img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="height:26px;width:auto;display:block;">
  </a>
  <div class="nav-links" id="navLinks">
    <a href="/#how">How it works</a>
    <a href="/pricing">Pricing</a>
    <a href="/faq">FAQ</a>
    <a href="/login">Log In</a>
  </div>
  <a href="/tc-check" class="nav-cta">Try TC Check Free</a>
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
  <h1>Free to check.<br><span class="gradient">Simple to scale.</span></h1>
  <p>Check any TREC 20-19 free. Pay only to draft offers by text, or to cover your whole brokerage.</p>
</div>

<div class="pricing-grid" style="max-width:1000px;">

  <div class="pricing-card" id="tc-check">
    <h2 class="plan-name">TC File Check</h2>
    <p class="plan-desc">See what title would kick back on any TREC 20-19.</p>
    <div class="price-row">
      <span class="price-current">Free</span>
    </div>
    <ul class="features">
      <li><span class="check">&#10003;</span> Blanks, missing initials and mismatches, itemized</li>
      <li><span class="check">&#10003;</span> Results in seconds</li>
      <li><span class="check">&#10003;</span> 3 full reports with no signup, then just your email</li>
      <li><span class="check">&#10003;</span> Your file isn&rsquo;t kept</li>
    </ul>
    <a href="/tc-check" class="cta-btn">Check a file free</a>
  </div>

  <div class="pricing-card" id="individual">
    <h2 class="plan-name">Agent</h2>
    <p class="plan-desc">Draft TREC 20-19 offers by text message.</p>
    <div class="price-row">
      <span class="price-current">$40</span>
      <span class="price-period">/month</span>
    </div>
    <ul class="features">
      <li><span class="check">&#10003;</span> First 3 offers free</li>
      <li><span class="check">&#10003;</span> Then unlimited offers</li>
      <li><span class="check">&#10003;</span> Address, price and closing date filled in for you</li>
      <li><span class="check">&#10003;</span> Every offer kept 5 years in your dashboard</li>
      <li><span class="check">&#10003;</span> Cancel anytime</li>
    </ul>
    <form action="/create-checkout-session" method="POST">
      <input type="hidden" name="plan" value="starter">
      <button type="submit" class="cta-btn">Subscribe &mdash; $40/mo</button>
    </form>
  </div>

  <div class="pricing-card featured" id="brokerage">
    <span class="featured-badge">For managing brokers</span>
    <h2 class="plan-name">Brokerage</h2>
    <p class="plan-desc">Every agent on your roster, covered.</p>
    <div class="price-row">
      <span class="price-current">$349</span>
      <span class="price-period">/month</span>
    </div>
    <ul class="features">
      <li><span class="check">&#10003;</span> Every agent&rsquo;s offer checked before it&rsquo;s sent</li>
      <li><span class="check">&#10003;</span> Unlimited offers for all your agents</li>
      <li><span class="check">&#10003;</span> Contract archive: forward or drop executed contracts, checked and kept 5 years</li>
      <li><span class="check">&#10003;</span> DocuSign, Transaction Workspace and roster dashboard</li>
    </ul>
    <a href="/brokers" class="cta-btn" data-evt="pricing_audit_cta">Start with a free 20-file audit</a>
    <form action="/create-checkout-session" method="POST" style="margin-top:0.75rem;text-align:center;">
      <input type="hidden" name="plan" value="brokerage">
      <button type="submit" style="background:none;border:none;padding:0;font:inherit;font-size:0.85rem;color:var(--text-muted);text-decoration:underline;cursor:pointer;">Ready now? Subscribe &mdash; $349/mo</button>
    </form>
  </div>

</div>

<p style="text-align:center;font-size:0.9rem;color:var(--text-muted);padding:0 1rem 2.5rem;">Multi-office brokerage or franchise? <a href="mailto:support@txtanoffer.com?subject=Multi-office%20pricing" style="color:var(--accent-dark);font-weight:600;">Contact us</a> for custom terms.</p>

<div class="value-section" style="border-top:1px solid var(--border);">
  <h2 style="text-align:center;font-size:1.4rem;font-weight:800;margin-bottom:1.5rem;color:var(--text);">Questions</h2>
  <div class="value-grid">
    <div class="value-card">
      <div class="value-title">Is TC File Check really free?</div>
      <div class="value-text">Yes. Your first 3 full reports need nothing at all. After that, add your email to keep getting full reports, still free. No card, no trial.</div>
    </div>
    <div class="value-card">
      <div class="value-title">Do you keep my client&rsquo;s file?</div>
      <div class="value-text">No. A file you check is deleted right after your report. If you get the report by email, its subject line includes the property address, and for bulk checks we keep each file&rsquo;s name and issue count, viewable at your batch link. The one exception is on purpose: on the Brokerage plan, contracts you forward or upload to your archive, and offers your agents text in, are kept 5 years for your records.</div>
    </div>
    <div class="value-card">
      <div class="value-title">Can I cancel?</div>
      <div class="value-text">Anytime, from your dashboard. No contracts. You keep access through the period you paid for.</div>
    </div>
  </div>
</div>

<div class="footer-note">
  By subscribing you agree to our <a href="/terms">Terms of Service</a>.
  <br><br>
  <a href="/tc-check">&larr; Try TC File Check</a> &middot; <a href="/">Home</a>
</div>

</body>
</html>
"""
    # Same first-touch ?src= attribution as the homepage (see that route's
    # comment) -- added 2026-09-11 because cold-outreach Brokerage pitches
    # (e.g. src=zillow_broker_reach) link straight to /pricing#brokerage,
    # never touching / or /tc-check, so without this the ta_src cookie
    # never gets set for that traffic and every resulting checkout/signup
    # silently misattributes as "direct" no matter how the visit is tagged.
    import re as _re
    src = _re.sub(r"[^a-zA-Z0-9_-]", "", request.args.get("src", ""))[:60]
    resp = make_response(html)
    if src and not request.cookies.get("ta_src"):
        resp.set_cookie("ta_src", src, max_age=30 * 24 * 3600, httponly=True, samesite="Lax")
        track_event("landing_visit", None, {"source": src})
    return resp


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
        return redirect("mailto:support@txtanoffer.com?subject=Early%20Adopter%20Signup")

    # Same ta_src first-touch cookie /signup and /pricing already read --
    # carried into Stripe's own session metadata so the webhook below can
    # attribute a self-serve Brokerage signup back to the campaign that
    # actually drove it (e.g. src=zillow_broker_reach), the same way
    # get_signups_by_source() already does for the agent /signup path.
    import re as _re
    checkout_src = _re.sub(r"[^a-zA-Z0-9_-]", "", request.cookies.get("ta_src", "") or "")[:60]

    # The brokerage plan provisions a real brokerage account (see
    # brokerages.py) on successful payment -- needs a company name Stripe's
    # checkout doesn't collect by default, so ask for it here via a custom
    # field. Not used by starter/professional.
    checkout_kwargs = dict(
        line_items=[{
            'price': price_id,
            'quantity': 1,
        }],
        mode='subscription',
        phone_number_collection={'enabled': True},
        success_url=request.host_url + 'success?session_id={CHECKOUT_SESSION_ID}',
        cancel_url=request.host_url + 'pricing',
        allow_promotion_codes=True,
        metadata={'plan': plan, 'source': checkout_src},
    )
    if plan == "brokerage":
        checkout_kwargs['custom_fields'] = [{
            'key': 'brokerage_name',
            'label': {'type': 'custom', 'custom': 'Brokerage / company name'},
            'type': 'text',
        }]

    try:
        checkout_session = stripe.checkout.Session.create(**checkout_kwargs)
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
    --accent:#0b5d52;--accent-light:#16806e;--accent-dark:#0a3a33;--accent-tint:#E7F3F1;
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
    background:linear-gradient(135deg,var(--accent),#0a3a33);color:#fff;
    text-decoration:none;border-radius:var(--radius-sm);font-weight:600;font-size:0.95rem;
    transition:all 0.2s ease;}}
  .btn:hover{{transform:translateY(-2px);box-shadow:0 8px 24px rgba(11,93,82,0.25);}}
  .logo{{margin-bottom:1.5rem;}}
  .logo img{{width:48px;height:48px;border-radius:22%;object-fit:contain;}}
</style>
</head>
<body>
  <div class="card">
    <div class="logo"><a href="/"><img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="height:30px;width:auto;"></a></div>
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
        session_metadata = session.get('metadata') or {}
        plan = session_metadata.get('plan', 'starter')
        checkout_source = session_metadata.get('source') or ''

        # Activate subscription on agent's phone number
        if customer_phone:
            user = get_user(customer_phone)
            if not user:
                create_user(customer_phone)
            activate_subscription(customer_phone, customer_id, subscription_id, plan=plan)

        # The brokerage plan self-provisions: a paid checkout creates the
        # brokerage account, links whoever paid (if they gave a phone) as
        # its first agent, and emails the join_code -- no manual step from
        # /admin/brokerages needed for a self-serve signup. Never let this
        # block subscription activation above if it fails.
        if plan == "brokerage":
            try:
                custom_fields = session.get('custom_fields') or []
                name_field = next((f for f in custom_fields if f.get('key') == 'brokerage_name'), None)
                brokerage_name = ((name_field or {}).get('text') or {}).get('value') or customer_email or "New Brokerage"
                # customer_phone is whoever paid for the Brokerage plan -- the
                # managing broker or their TC, in the self-serve case -- so it
                # doubles as the SMS alert number with no extra signup step.
                brokerage = create_brokerage(brokerage_name, customer_email or "", customer_phone or "", checkout_source)
                if customer_phone:
                    link_user_to_brokerage(customer_phone, brokerage["id"])
                if customer_email:
                    sms_alert_line = (
                        f"We'll also text {customer_phone} the moment one of your agents' offers has "
                        f"a blocker (missing county, title company, escrow agent, etc.) -- reply STOP on "
                        f"that number anytime to turn texted alerts off; the email alerts above keep going.\n\n"
                        if customer_phone else ""
                    )
                    send_plain_email(
                        customer_email,
                        f"{brokerage_name} is set up on TxtAnOffer",
                        (
                            f"Your brokerage join code: {brokerage['join_code']}\n\n"
                            f"Give this to your agents. On their first offer, they text it as a prefix:\n"
                            f"{brokerage['join_code']} 725k 3% 21day 123 Main St\n\n"
                            f"Or they can enter it at signup: {request.host_url.rstrip('/')}/signup\n\n"
                            f"Once linked, every offer they draft auto-emails this address with the PDF "
                            f"and a compliance check -- no login needed. {sms_alert_line}"
                            f"To browse the full roster and "
                            f"activity, use: {request.host_url.rstrip('/')}/broker/dashboard/{brokerage['join_code']}\n\n"
                            f"Keep this email -- the join code is also your dashboard link's access key."
                        ),
                    )
                track_event("brokerage_created", customer_phone, {
                    "brokerage_id": brokerage["id"], "name": brokerage_name, "via": "stripe_checkout",
                    "source": checkout_source or "direct",
                })
            except Exception as e:
                print(f"[WEBHOOK] brokerage auto-provision failed: {e}")

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


@app.route("/internal-mode")
def internal_mode():
    """Flags this browser as the site owner's own traffic, silently
    excluded from every /analytics metric from then on (see
    analytics.py's track_event/_is_internal_traffic). Visit once per
    browser/device: /internal-mode?token=<ANALYTICS_PASSWORD> to turn on,
    &off=1 to turn back off (e.g. to see what a real visitor actually
    sees). Deliberately cookie-based, not IP-based -- home wifi, phone
    LTE, and a coffee shop are three different IPs for the same person,
    so an IP allowlist would need constant upkeep and silently stop
    working the moment it's stale. Reuses ANALYTICS_PASSWORD rather than
    a second secret to manage."""
    if not ANALYTICS_PASSWORD:
        abort(404)
    token = request.args.get("token", "")
    if not hmac.compare_digest(token, ANALYTICS_PASSWORD):
        abort(404)
    turning_off = request.args.get("off") == "1"
    resp = make_response(
        "Internal mode is now OFF for this browser -- your visits will count in /analytics again."
        if turning_off else
        "Internal mode is now ON for this browser -- your visits will no longer count in /analytics."
    )
    if turning_off:
        resp.delete_cookie("ta_internal")
    else:
        resp.set_cookie("ta_internal", ANALYTICS_PASSWORD, max_age=365 * 24 * 3600, httponly=True, samesite="Lax")
    # Also flag this browser's anonymous visitor id, so page views it logged
    # before internal mode was turned on drop out of the Daily Funnel too.
    visitor = request.cookies.get("ta_vid", "")
    if re.fullmatch(r"[0-9a-f]{32}", visitor):
        set_internal_visitor(visitor, not turning_off)
    return resp


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
    daily_funnel = get_daily_funnel(days=14)
    visitor_roles = get_visitor_roles()
    engagement_by_device = get_engagement_by_device(days=7)
    recent_visitors = get_recent_visitors(hours=48)
    archive_ea_rows = "".join(
        f"<tr><td style='padding:6px;'>{escape(r['created_at'][:16].replace('T',' '))}</td><td style='padding:6px;'>{escape(r['email'])}</td>"
        f"<td style='padding:6px;'>{escape(r['brokerage'])}</td><td style='padding:6px;'>{escape(r['agents'])}</td><td style='padding:6px;'>{escape(r['source'])}</td></tr>"
        for r in get_archive_early_access()
    ) or "<tr><td colspan='5' style='padding:6px;color:#999;'>No sign-ups yet.</td></tr>"
    top_referrers = get_top_referrers(days=30)
    tc_check_attempts_by_source = get_tc_check_attempts_by_source(days=30)
    tc_check_attempts_by_page = get_tc_check_attempts_by_page(days=30)
    tc_check_summary = get_tc_check_summary(days=30)
    recent_tc_email_senders = get_recent_tc_check_email_senders(limit=20)
    tc_repeat_senders = get_tc_check_repeat_senders(within_days=14)
    tc_bulk_summary = get_tc_check_bulk_summary(days=30)
    brokerage_alert_delivery = get_brokerage_alert_delivery(days=30)

    # Every 30-day metric above answers "how are we doing overall" but not
    # "did anything happen since yesterday" -- that used to require manually
    # eyeballing timestamps in the raw activity tables. Re-running the same
    # days-scoped functions with days=1 gives a true trailing-24h window
    # (not a calendar-day bucket) for free, since a days=1 cutoff is always
    # a strict subset of the days=30 one -- no new query logic needed.
    metrics_24h = get_conversion_metrics(days=1)
    signups_by_source_24h = get_signups_by_source(days=1)
    landing_visits_by_source_24h = get_landing_visits_by_source(days=1)
    tc_check_attempts_by_source_24h = get_tc_check_attempts_by_source(days=1)
    tc_check_attempts_by_page_24h = get_tc_check_attempts_by_page(days=1)
    tc_check_summary_24h = get_tc_check_summary(days=1)
    tc_bulk_summary_24h = get_tc_check_bulk_summary(days=1)
    brokerage_alert_delivery_24h = get_brokerage_alert_delivery(days=1)

    def _merge_24h_counts(rows_30, rows_24, key_field):
        """rows_24's keys are always a subset of rows_30's (see comment
        above), so this only needs to look up rows_30's ordering and fill
        in 0 for anything with no activity in the last 24h."""
        by_key_24 = {r[key_field]: r["count"] for r in rows_24}
        return [{**r, "count_24h": by_key_24.get(r[key_field], 0)} for r in rows_30]

    signups_by_source_merged = _merge_24h_counts(signups_by_source, signups_by_source_24h, "source")
    landing_visits_by_source_merged = _merge_24h_counts(landing_visits_by_source, landing_visits_by_source_24h, "source")
    tc_check_attempts_by_source_merged = _merge_24h_counts(tc_check_attempts_by_source, tc_check_attempts_by_source_24h, "source")
    tc_check_attempts_by_page_merged = _merge_24h_counts(tc_check_attempts_by_page, tc_check_attempts_by_page_24h, "page")
    tc_issue_frequency_merged = _merge_24h_counts(tc_check_summary["issue_frequency"], tc_check_summary_24h["issue_frequency"], "key")

    tc_email_sender_rows = ""
    for entry in recent_tc_email_senders:
        from datetime import datetime as _dt
        dt = _dt.fromisoformat(entry['created_at'])
        time_str = dt.strftime("%m/%d %H:%M")
        known_str = "known" if entry['known_sender'] else "new"
        recognized_str = "yes" if entry['recognized'] else "no"
        if entry.get("dropped"):
            result_str = f"<span style='color:#b45309;'>dropped, no reply ({escape(entry['reason'])})</span>"
        else:
            result_str = ("recognized" if entry['recognized'] else f"replied, not recognized ({escape(entry['reason'])})" if entry['reason'] else "replied, not recognized")
        tc_email_sender_rows += (
            f"<tr><td>{time_str}</td><td>{escape(entry['sender'])}</td>"
            f"<td>{known_str}</td><td>{result_str}</td></tr>"
        )

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
        f"<tr><td>{s['source']}</td><td>{s['count']}</td><td>{s['count_24h']}</td></tr>" for s in signups_by_source_merged
    ) or '<tr><td colspan="3" style="padding:10px;color:#666;">No signups yet.</td></tr>'
    visit_rows = "".join(
        f"<tr><td>{v['source']}</td><td>{v['count']}</td><td>{v['count_24h']}</td></tr>" for v in landing_visits_by_source_merged
    ) or '<tr><td colspan="3" style="padding:10px;color:#666;">No tagged visits yet.</td></tr>'
    daily_rows = "".join(
        f"<tr><td>{d['date'][5:]}</td><td><strong>{d['js_ok']}</strong></td><td style='color:#999;'>{d['visitors']}</td><td>{d['mobile']}</td><td>{d['stay_10s']}</td><td>{d['hero_cta']}</td>"
        f"<td>{d['hero_sample']}</td><td>{d['dropzone_seen']}</td><td>{d['attempts']}</td>"
        f"<td>{d['demos']}</td><td>{d['recognized']}</td><td>{d['emails_captured']}</td><td>{d['email_checks']}</td><td>{d['email_junk']}</td></tr>"
        for d in daily_funnel
    )
    role_rows = "".join(
        f"<tr><td>{r['label']}</td><td>{r['count']}</td><td>{'' if r['pct'] is None else str(r['pct']) + '%'}</td>"
        f"<td>{r['dropzone']}</td><td>{r['demoed']}</td><td>{r['uploaded']}</td><td>{r['recognized']}</td><td>{r['repeat']}</td></tr>"
        for r in visitor_roles.get("roles", [])
    )
    if visitor_roles["since"]:
        _target = sum(r["count"] for r in visitor_roles["roles"] if r["key"] in ("tc", "broker"))
        role_summary = (
            f"Since {visitor_roles['since']}: {visitor_roles['visitors']} visitors &rarr; {visitor_roles['real']} real browsers "
            f"&rarr; card shown to {visitor_roles['shown']} &rarr; {visitor_roles['answered']} answered "
            f"({visitor_roles['dismissed']} closed it). <strong>TC + Broker: {_target} of {visitor_roles['answered']} answers.</strong>"
        )
    else:
        role_summary = "Card hasn't been shown to anyone yet."
    def _central(ts):
        try:
            from zoneinfo import ZoneInfo
            from datetime import datetime as _utc_dt
            return _utc_dt.fromisoformat(ts).replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/Chicago")).strftime("%m-%d %I:%M %p")
        except Exception:
            return ts[5:16]

    def _visitor_row(v):
        # js_ok only exists from 2026-10-06; any engagement beacon (drop box
        # seen, 10s stay...) also proves the page's JS ran, so older rows count.
        real = bool(v["events"] - {""})
        engaged = [label for key, label in (("stay_10s", "10s+"), ("stay_60s", "60s+"), ("dropzone_seen", "saw drop box"),
                                            ("hero_cta", "clicked check"), ("hero_sample", "sample"))
                   if key in v["events"]]
        engaged += [e.replace("_", " ") for e in v["events"] if e.endswith("_cta") and e != "hero_cta"]
        engaged += [e[5:] for e in v["events"] if e.startswith("role_") and e not in ("role_shown", "role_dismissed")]
        pages = ", ".join(f"{p}&times;{n}" if n > 1 else p for p, n in v["pages"].items()) or "(beacon only)"
        style = "" if real else "color:#999;"
        return (f"<tr style='{style}'><td style='padding:6px;white-space:nowrap;'>{_central(v['first'])}</td>"
                f"<td style='padding:6px;'>{escape(pages)}</td>"
                f"<td style='padding:6px;'>{escape(', '.join(v['referrers']))}</td>"
                f"<td style='padding:6px;'>{escape(', '.join(v['sources']))}</td>"
                f"<td style='padding:6px;'>{escape(', '.join(v['devices']))}</td>"
                f"<td style='padding:6px;'>{'&#10003;' if real else '&mdash;'}</td>"
                f"<td style='padding:6px;'>{escape(', '.join(engaged))}</td>"
                f"<td style='padding:6px;font-size:11px;max-width:260px;word-break:break-all;'>{escape(v['ua'] or '(not logged)')}</td></tr>")

    recent_visitor_rows = "".join(_visitor_row(v) for v in recent_visitors) or \
        "<tr><td colspan='8' style='padding:6px;color:#666;'>No visitors in the last 48 hours.</td></tr>"
    recent_real = sum(1 for v in recent_visitors if v["events"] - {""})

    device_rows = "".join(
        f"<tr><td>{d['device']}</td><td><strong>{d['js_ok']}</strong></td><td style='color:#999;'>{d['visitors']}</td><td>{d['stay_10s']}</td>"
        f"<td>{d['dropzone_seen']}</td><td>{d['hero_cta']}</td></tr>"
        for d in engagement_by_device
    )
    send_failure_rows = "".join(
        f"<tr><td>{f['time'][5:16].replace('T', ' ')}</td><td>{escape(f['to'])}</td><td>{escape(f['subject'])}</td><td>{escape(f['error'])}</td></tr>"
        for f in get_recent_email_send_failures(limit=10)
    ) or '<tr><td colspan="4" style="padding:10px;color:#666;">No failed sends recorded.</td></tr>'
    referrer_rows = "".join(
        f"<tr><td>{escape(r['referrer'])}</td><td>{r['count']}</td></tr>" for r in top_referrers
    ) or '<tr><td colspan="2" style="padding:10px;color:#666;">No page views recorded yet.</td></tr>'
    tc_attempt_rows = "".join(
        f"<tr><td>{a['source']}</td><td>{a['count']}</td><td>{a['count_24h']}</td></tr>" for a in tc_check_attempts_by_source_merged
    ) or '<tr><td colspan="3" style="padding:10px;color:#666;">No attempts yet.</td></tr>'
    tc_attempt_page_rows = "".join(
        f"<tr><td>{a['page']}</td><td>{a['count']}</td><td>{a['count_24h']}</td></tr>" for a in tc_check_attempts_by_page_merged
    ) or '<tr><td colspan="3" style="padding:10px;color:#666;">No attempts yet.</td></tr>'
    tc_issue_rows = "".join(
        f"<tr><td>{i['label']}</td><td>{i['count']}</td><td>{i['pct_of_recognized']}%</td><td>{i['count_24h']}</td></tr>"
        for i in tc_issue_frequency_merged
    ) or '<tr><td colspan="4" style="padding:10px;color:#666;">No checks recognized yet.</td></tr>'
    _alerts_24h_by_source = {r["source"]: r["alerts"] for r in brokerage_alert_delivery_24h["by_source"]}
    brokerage_alert_rows = "".join(
        f"<tr><td>{r['source']}</td><td>{r['alerts']}</td><td>{r['sms_sent']}</td>"
        f"<td>{r['sms_eligible_no_phone']}</td><td>{_alerts_24h_by_source.get(r['source'], 0)}</td></tr>"
        for r in brokerage_alert_delivery["by_source"]
    ) or '<tr><td colspan="5" style="padding:10px;color:#666;">No brokerage-linked offers drafted yet.</td></tr>'
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
.h24{{margin-top:6px;font-size:13px;color:#A9772F;font-weight:600;}}
</style></head><body>
<h1>TxtAnOffer Analytics</h1>
<h2>Last 30 Days</h2>
<div class="metric">
  <h3>Daily Funnel (last 14 days, UTC)</h3>
  <div style="overflow-x:auto;">
  <table style="width:100%;border-collapse:collapse;margin-top:10px;font-size:14px;">
    <tr style="background:#eee;text-align:left;">
      <th style="padding:6px;">Day</th>
      <th style="padding:6px;">Real visitors</th>
      <th style="padding:6px;color:#999;">Page loads (incl. bots)</th>
      <th style="padding:6px;">Loads on phone</th>
      <th style="padding:6px;">Stayed 10s+</th>
      <th style="padding:6px;">Clicked &ldquo;Check a file&rdquo;</th>
      <th style="padding:6px;">Hero sample</th>
      <th style="padding:6px;">Saw drop box</th>
      <th style="padding:6px;">Widget attempts</th>
      <th style="padding:6px;">Sample used</th>
      <th style="padding:6px;">Recognized</th>
      <th style="padding:6px;">Emails captured</th>
      <th style="padding:6px;">Email checks</th>
      <th style="padding:6px;">Junk filtered</th>
    </tr>
    {daily_rows}
  </table>
  </div>
  <p class="label" style="margin-top:8px;"><strong>Real visitors</strong> = browsers that actually ran the page (from 2026-10-06; earlier days show 0) &mdash; this is the number to watch. <strong>Page loads (incl. bots)</strong> = every load of <code>/</code> or <code>/tc-check</code> not caught by the bot filter; link scanners and crawlers that pose as browsers land here and don&rsquo;t keep cookies, so each of their hits counts again. Stayed / clicked / saw drop box are unique browsers (tracked from 2026-10-03). Real visitors minus &ldquo;Stayed 10s+&rdquo; &asymp; bounced. &ldquo;Sample used&rdquo; counts every sample run (hero button or the one under the drop box). Widget attempts = real uploads, excluding the sample. Email checks exclude junk senders from 2026-10-02 on.</p>
</div>
<div class="metric">
  <h3>What Brings You Here? (self-ID card)</h3>
  <p style="color:#666;font-size:13px;">{role_summary}</p>
  <table><tr><th>Role</th><th>Visitors</th><th>% of answers</th><th>Saw drop box</th><th>Tried sample</th><th>Uploaded</th><th>Recognized 20-19</th><th>Repeat (2+)</th></tr>{role_rows}</table>
  <p class="label" style="margin-top:8px;">One row per browser (ta_vid cookie), counted from the first time the card was shown. &ldquo;No answer&rdquo; = real browsers that never answered (left before 4s, closed it, or ignored it). Repeat = 2+ recognized uploads from the same browser.</p>
</div>
<div class="metric">
  <h3>Archive Early Access Sign-ups</h3>
  <p style="color:#666;font-size:13px;">From /archive. Demand check for the drop/forward contract archive before building it.</p>
  <table><tr><th>Time (UTC)</th><th>Email</th><th>Brokerage</th><th>Agents</th><th>Source</th></tr>{archive_ea_rows}</table>
</div>
<div class="metric">
  <h3>Recent Visitors (48 hours, Central time)</h3>
  <p style="color:#666;font-size:13px;">{len(recent_visitors)} visitors &middot; {recent_real} ran the page in a real browser. Grey rows never ran the page's JavaScript &mdash; almost always link scanners or preview bots.</p>
  <div style="overflow-x:auto;">
  <table><tr><th>First seen</th><th>Pages</th><th>Referrer</th><th>Source</th><th>Device</th><th>Real browser</th><th>Did</th><th>Browser (user agent)</th></tr>{recent_visitor_rows}</table>
  </div>
  <p class="label" style="margin-top:8px;">One row per browser cookie. Bots don't keep cookies, so each bot hit shows up as its own one-page row. Your own visits are excluded once you've opened /internal-mode on that device. User agent is logged from 2026-10-07; older rows say "(not logged)".</p>
</div>
<div class="metric">
  <h3>Tracking Check: Phone vs Desktop (7 days)</h3>
  <table><tr><th>Device</th><th>Real visitors</th><th style="color:#999;">Page loads (incl. bots)</th><th>Stayed 10s+</th><th>Saw drop box</th><th>Clicked &ldquo;Check a file&rdquo;</th></tr>{device_rows}</table>
  <p class="label" style="margin-top:8px;">If phones show visitors but ~0 in every other column while desktop doesn't, suspect a tracking bug on mobile rather than visitor behavior.</p>
</div>

<div class="metric">
  <h3>Top Referrers (30 days)</h3>
  <table style="width:100%;border-collapse:collapse;margin-top:10px;">
    <tr style="background:#eee;text-align:left;">
      <th style="padding:8px;">Referring site</th>
      <th style="padding:8px;">Page views</th>
    </tr>
    {referrer_rows}
  </table>
  <p class="label" style="margin-top:8px;">"(none)" = typed URL, bookmark, or an app that strips the referrer (LinkedIn's mobile app does).</p>
</div>
<div class="metric">
  <h3>Conversion Funnel</h3>
  <div class="value">{metrics['overall_conversion_rate']}%</div>
  <div class="label">Free → Paid Conversion Rate</div>
  <p>{metrics['signups']} signups → {metrics['conversions']} paid</p>
  <p class="h24">Last 24h: {metrics_24h['signups']} signups &rarr; {metrics_24h['conversions']} paid</p>
</div>
<div class="metric">
  <h3>Signups by Source (30 days)</h3>
  <table style="width:100%;border-collapse:collapse;margin-top:10px;">
    <tr style="background:#eee;text-align:left;">
      <th style="padding:8px;">Source</th>
      <th style="padding:8px;">30 Days</th>
      <th style="padding:8px;">Last 24h</th>
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
      <th style="padding:8px;">30 Days</th>
      <th style="padding:8px;">Last 24h</th>
    </tr>
    {visit_rows}
  </table>
  <p class="label" style="margin-top:8px;">Raw clicks on a <code>?src=</code> link, counted even if the visitor never signs up &mdash; tells you whether a channel is being opened at all vs. opened-but-not-converting.</p>
</div>
<div class="metric">
  <h3>TC File Check &mdash; Widget Attempts by Source (30 days)</h3>
  <table style="width:100%;border-collapse:collapse;margin-top:10px;">
    <tr style="background:#eee;text-align:left;">
      <th style="padding:8px;">Source</th>
      <th style="padding:8px;">30 Days</th>
      <th style="padding:8px;">Last 24h</th>
    </tr>
    {tc_attempt_rows}
  </table>
  <p class="label" style="margin-top:8px;">Every real submission to the file checker for that source, success or error &mdash; compare against the visit count above: a big gap means visitors aren't engaging the widget at all; attempts close to visits but low recognized/complete below means they're trying and something's failing.</p>
</div>
<div class="metric">
  <h3>TC File Check &mdash; Widget Attempts by Page (30 days)</h3>
  <table style="width:100%;border-collapse:collapse;margin-top:10px;">
    <tr style="background:#eee;text-align:left;">
      <th style="padding:8px;">Page</th>
      <th style="padding:8px;">30 Days</th>
      <th style="padding:8px;">Last 24h</th>
    </tr>
    {tc_attempt_page_rows}
  </table>
  <p class="label" style="margin-top:8px;">Same submissions as above, grouped by which UI was actually used &mdash; homepage widget vs the dedicated /tc-check page &mdash; instead of which channel drove the visit. "unknown" is anything tracked before 2026-09-11 or missing the tag.</p>
</div>
<div class="metric">
  <h3>Trial Activation</h3>
  <div class="value">{metrics['trial_activation_rate']}%</div>
  <div class="label">Users who complete 3 free offers</div>
  <p>{metrics['trial_completions']} / {metrics['signups']} users</p>
  <p class="h24">Last 24h: {metrics_24h['trial_completions']} / {metrics_24h['signups']} users</p>
</div>
<div class="metric">
  <h3>Paywall → Paid</h3>
  <div class="value">{metrics['paywall_to_paid_rate']}%</div>
  <div class="label">Users who pay after hitting limit</div>
  <p>{metrics['conversions']} / {metrics['hit_paywall']} users</p>
  <p class="h24">Last 24h: {metrics_24h['conversions']} / {metrics_24h['hit_paywall']} users</p>
</div>
<div class="metric">
  <h3>Usage</h3>
  <div class="value">{metrics['total_offers']}</div>
  <div class="label">Total offers generated</div>
  <p>{metrics['avg_offers_per_user']} offers per user average</p>
  <p class="h24">Last 24h: {metrics_24h['total_offers']} offers generated</p>
</div>
<div class="metric">
  <h3>TC File Check</h3>
  <div class="value">{tc_check_summary['total']}</div>
  <div class="label">Files checked across both channels (30 days)</div>
  <p>{tc_check_summary['recognized']} recognized as a TREC 20-19 &middot; {tc_check_summary['complete']} came back complete ({tc_check_summary['completion_rate']}%)</p>
  <p class="h24">Last 24h: {tc_check_summary_24h['total']} checked &middot; {tc_check_summary_24h['recognized']} recognized &middot; {tc_check_summary_24h['complete']} complete</p>
</div>
<div class="metric">
  <h3>TC File Check &mdash; Web vs. Email</h3>
  <div class="value">{tc_check_summary['web_count']} / {tc_check_summary['email_count']}</div>
  <div class="label">Web uploads (/tc-check) vs. forwarded to tc@check.txtanoffer.com</div>
  <p>{tc_check_summary['email_new_sender_pct']}% of email forwards came from a sender not already in agent_profiles &mdash; {tc_check_summary['email_known_sender']} of {tc_check_summary['email_count']} were known</p>
  <p class="h24">Last 24h: {tc_check_summary_24h['web_count']} web / {tc_check_summary_24h['email_count']} email</p>
</div>
<div class="metric" style="grid-column:1/-1;">
  <h3>Outbound Email Failures</h3>
  <div class="label">Emails SendGrid refused to send (TC Check replies, reports, notifications). Empty = every send since 2026-10-07 was accepted by SendGrid.</div>
  <table><tr><th>Time (UTC)</th><th>To</th><th>Subject</th><th>SendGrid error</th></tr>{send_failure_rows}</table>
</div>
<div class="metric" style="grid-column:1/-1;">
  <h3>TC File Check &mdash; Recent Email Senders</h3>
  <div class="label">Cross-reference against an outreach list's email addresses to attribute a forward to a specific campaign &mdash; raw event log has no other way to answer that.</div>
  <table style="width:100%;border-collapse:collapse;margin-top:8px;">
    <tr style="background:#f5f5f5;text-align:left;">
      <th style="padding:8px;">Time</th>
      <th style="padding:8px;">Sender</th>
      <th style="padding:8px;">Known?</th>
      <th style="padding:8px;">Result</th>
    </tr>
    {tc_email_sender_rows if tc_email_sender_rows else '<tr><td colspan="4" style="padding:8px;color:#999;">No email forwards yet.</td></tr>'}
  </table>
</div>
<div class="metric">
  <h3>TC File Check &rarr; Email Capture</h3>
  <div class="value">{tc_check_summary['email_capture_rate']}%</div>
  <div class="label">Of web uploads (/tc-check), gave an email (opt-in checkbox before upload, or to unlock the full report)</div>
  <p>{tc_check_summary['emails_captured']} emails / {tc_check_summary['web_count']} web checks &mdash; since 2026-10-02 each browser gets {TC_FREE_FULL_REPORTS} free full reports before the email gate (was: preview-only from the first check, 2026-09-10)</p>
  <p class="h24">Last 24h: {tc_check_summary_24h['emails_captured']} / {tc_check_summary_24h['web_count']}</p>
</div>
<div class="metric">
  <h3>TC File Check &rarr; Repeat Checkers</h3>
  <div class="value">{tc_repeat_senders['returned_within_window']} / 10</div>
  <div class="label">Distinct emails that came back for a 2nd check within {tc_repeat_senders['window_days']} days (lifetime, not 30-day)</div>
  <p>{tc_repeat_senders['distinct_senders']} distinct identified senders total &mdash; goal is 10+ before revisiting pricing</p>
</div>
<div class="metric">
  <h3>TC File Check &mdash; Top Issues (30 days)</h3>
  <table style="width:100%;border-collapse:collapse;margin-top:10px;">
    <tr style="background:#eee;text-align:left;">
      <th style="padding:8px;">Issue</th>
      <th style="padding:8px;">Count</th>
      <th style="padding:8px;">% of recognized files</th>
      <th style="padding:8px;">Last 24h</th>
    </tr>
    {tc_issue_rows}
  </table>
  <p class="label" style="margin-top:8px;">Whatever's at the top of this list is both the next marketing hook ("audited N files, X% are missing...") and a candidate for a dedicated feature or reminder.</p>
</div>
<div class="metric">
  <h3>Bulk TC File Check</h3>
  <div class="value">{tc_bulk_summary['batches']}</div>
  <div class="label">Batches submitted at /tc-check/bulk (30 days)</div>
  <p>{tc_bulk_summary['total_files']} files checked total &middot; {tc_bulk_summary['with_issues']} of {tc_bulk_summary['recognized']} recognized files had at least one issue ({tc_bulk_summary['with_issues_pct']}%)</p>
  <p>{tc_bulk_summary['free_batches']} free-tier batches (capped at {FREE_BULK_LIMIT} files) &middot; {tc_bulk_summary['brokerage_batches']} with a Brokerage join code (up to {MAX_BULK_FILES})</p>
  <p class="h24">Last 24h: {tc_bulk_summary_24h['batches']} batches &middot; {tc_bulk_summary_24h['total_files']} files &middot; {tc_bulk_summary_24h['brokerage_batches']} brokerage-tier</p>
</div>
<div class="metric">
  <h3>Brokerage Alert Delivery by Source (30 days)</h3>
  <div class="value">{brokerage_alert_delivery['brokerages_with_sms']} / {brokerage_alert_delivery['brokerages_with_sms'] + brokerage_alert_delivery['brokerages_missing_phone']}</div>
  <div class="label">Linked brokerages with a TC phone on file (getting SMS alerts, not just email)</div>
  <table style="width:100%;border-collapse:collapse;margin-top:10px;">
    <tr style="background:#eee;text-align:left;">
      <th style="padding:8px;">Source (?src=)</th>
      <th style="padding:8px;">Alerts Sent</th>
      <th style="padding:8px;">SMS Sent</th>
      <th style="padding:8px;">SMS-Eligible, No Phone</th>
      <th style="padding:8px;">Last 24h</th>
    </tr>
    {brokerage_alert_rows}
  </table>
  <p class="label" style="margin-top:8px;">"Alerts Sent" fires every time a brokerage-linked agent drafts an offer (email always goes out). "SMS Sent" is the subset with a blocker AND a tc_phone on file. "SMS-Eligible, No Phone" is a blocker that would have texted if the brokerage had a phone on file -- add one at /admin/brokerages to convert those.</p>
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


@app.route("/admin/signups")
def admin_signups():
    """Diagnostic drill-down behind /analytics's aggregate signup counts --
    lists the actual phone/name/email/source per signup event so a handful
    of records can be reviewed individually (e.g. to reach out personally)
    instead of only seeing a total."""
    if not ANALYTICS_PASSWORD:
        abort(503)
    token = request.args.get("token", "")
    if not hmac.compare_digest(token, ANALYTICS_PASSWORD):
        abort(403)

    days = request.args.get("days", "30")
    signups = get_signup_details(days=int(days))

    rows = "".join(
        f"<tr><td style='padding:8px;'>{s['created_at'][:16].replace('T', ' ')}</td>"
        f"<td style='padding:8px;'>{s['phone'] or '&mdash;'}</td>"
        f"<td style='padding:8px;'>{s['name'] or '&mdash;'}</td>"
        f"<td style='padding:8px;'>{s['email'] or '&mdash;'}</td>"
        f"<td style='padding:8px;'>{s['source']}</td></tr>"
        for s in signups
    ) or "<tr><td colspan='5' style='padding:8px;color:#666;'>No signups in this window.</td></tr>"

    return f"""<!DOCTYPE html>
<html><head><title>Signups — TxtAnOffer Admin</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>body{{font-family:-apple-system,sans-serif;max-width:900px;margin:40px auto;padding:0 20px;color:#0b5d52;}}
table{{width:100%;border-collapse:collapse;margin-top:20px;}}
th{{text-align:left;padding:8px;border-bottom:2px solid #eee;}}
td{{border-bottom:1px solid #eee;}}</style>
</head><body>
<h1>Signups (last {days} days)</h1>
<table>
<tr><th>Time (UTC)</th><th>Phone</th><th>Name</th><th>Email</th><th>Source</th></tr>
{rows}
</table>
</body></html>"""

@app.route("/admin/brokerages", methods=["GET", "POST"])
def admin_brokerages():
    """Hand-provision a brokerage account: no self-serve signup exists yet
    (or is planned soon) -- this is run once per closed B2B deal to generate
    the join_code you hand the broker. Reuses ANALYTICS_PASSWORD rather than
    a second secret."""
    if not ANALYTICS_PASSWORD:
        abort(503)
    token = request.args.get("token", "") or request.form.get("token", "")
    if not hmac.compare_digest(token, ANALYTICS_PASSWORD):
        abort(403)

    created = None
    error = None
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        tc_email = request.form.get("tc_email", "").strip()
        tc_phone = request.form.get("tc_phone", "").strip()
        source = request.form.get("source", "").strip()
        if not name:
            error = "Brokerage name is required."
        else:
            created = create_brokerage(name, tc_email, tc_phone, source)
            if created:
                track_event("brokerage_created", tc_phone or None, {
                    "brokerage_id": created["id"], "name": name, "via": "admin_manual",
                    "source": source or "direct",
                })

    rows = "".join(
        f"<tr><td style='padding:8px;'>{b['name']}</td>"
        f"<td style='padding:8px;'>{b.get('tc_email') or '&mdash;'}</td>"
        f"<td style='padding:8px;'>{b.get('tc_phone') or '&mdash;'}</td>"
        f"<td style='padding:8px;'>{b.get('source') or '&mdash;'}</td>"
        f"<td style='padding:8px;font-family:monospace;font-weight:700;'>{b['join_code']}</td>"
        f"<td style='padding:8px;'><a href='/broker/dashboard/{b['join_code']}?token={token}'>dashboard &rarr;</a></td>"
        f"<td style='padding:8px;color:#666;'>{b['created_at'][:10]}</td></tr>"
        for b in list_brokerages()
    ) or "<tr><td colspan='7' style='padding:8px;color:#666;'>No brokerages yet.</td></tr>"

    created_banner = ""
    if created:
        created_banner = (
            f"<div style='background:#eafaf1;border:1px solid #2ecc71;border-radius:8px;padding:14px;margin-bottom:20px;'>"
            f"<strong>{created['name']}</strong> created. Join code: "
            f"<span style='font-family:monospace;font-weight:700;font-size:1.1em;'>{created['join_code']}</span><br>"
            f"Give this to their agents: text it as a prefix on the first offer (e.g. "
            f"<code>{created['join_code']} 725k 3% 21day 123 Main St</code>), or paste it into the "
            f"\"Brokerage join code\" field at /signup."
            f"</div>"
        )
    error_banner = f"<div style='color:#c0392b;margin-bottom:16px;'>{error}</div>" if error else ""

    return f"""<!DOCTYPE html>
<html><head><title>Brokerages — TxtAnOffer Admin</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>body{{font-family:-apple-system,sans-serif;max-width:900px;margin:40px auto;padding:0 20px;color:#0b5d52;}}
input{{padding:8px 10px;border:1px solid #ccc;border-radius:6px;font-size:0.9rem;margin-right:8px;}}
button{{padding:8px 16px;border:none;border-radius:6px;background:#0b5d52;color:#fff;font-weight:600;cursor:pointer;}}
table{{width:100%;border-collapse:collapse;margin-top:20px;}}
th{{text-align:left;padding:8px;border-bottom:2px solid #eee;}}
td{{border-bottom:1px solid #eee;}}
a{{color:#0b5d52;}}</style>
</head><body>
<h1>Brokerages</h1>
{created_banner}{error_banner}
<form method="POST">
  <input type="hidden" name="token" value="{token}">
  <input type="text" name="name" placeholder="Brokerage name" required>
  <input type="email" name="tc_email" placeholder="TC email for auto-CC (optional)">
  <input type="tel" name="tc_phone" placeholder="TC phone for SMS alerts (optional)">
  <input type="text" name="source" placeholder="Campaign source, e.g. zillow_broker_reach (optional)">
  <button type="submit">Create</button>
</form>
<table>
<tr><th>Name</th><th>TC email</th><th>TC phone</th><th>Source</th><th>Join code</th><th></th><th>Created</th></tr>
{rows}
</table>
</body></html>"""


@app.route("/admin/sponsors", methods=["GET", "POST"])
def admin_sponsors():
    """Hand-provision a title/lender sponsor: their branding page gets
    appended to every generated contract for a property in one of their
    counties (see sponsors.py, cover_page.generate_sponsor_page). No
    exclusivity is enforced here -- don't sell the same county twice."""
    if not ANALYTICS_PASSWORD:
        abort(503)
    token = request.args.get("token", "") or request.form.get("token", "")
    if not hmac.compare_digest(token, ANALYTICS_PASSWORD):
        abort(403)

    created = None
    error = None
    if request.method == "POST":
        action = request.form.get("action", "create")
        if action == "toggle":
            sponsor_id = int(request.form.get("sponsor_id", 0))
            active = request.form.get("active") == "1"
            set_sponsor_active(sponsor_id, active)
        else:
            name = request.form.get("name", "").strip()
            counties_raw = request.form.get("counties", "").strip()
            tagline = request.form.get("tagline", "").strip()
            contact_phone = request.form.get("contact_phone", "").strip()
            contact_email = request.form.get("contact_email", "").strip()
            counties = [c.strip() for c in counties_raw.split(",") if c.strip()]
            if not name:
                error = "Sponsor name is required."
            elif not counties:
                error = "At least one county is required (comma-separated)."
            else:
                created = create_sponsor(name, counties, tagline, contact_phone, contact_email)

    def sponsor_row(s):
        toggle_label = "Pause" if s["active"] else "Activate"
        toggle_next = "0" if s["active"] else "1"
        status = "<span style='color:#2ecc71;'>active</span>" if s["active"] else "<span style='color:#999;'>paused</span>"
        return (
            f"<tr><td style='padding:8px;'>{s['name']}</td>"
            f"<td style='padding:8px;'>{', '.join(c.title() for c in s['counties'])}</td>"
            f"<td style='padding:8px;'>{s.get('contact_email') or s.get('contact_phone') or '&mdash;'}</td>"
            f"<td style='padding:8px;'>{status}</td>"
            f"<td style='padding:8px;'>"
            f"<form method='POST' style='display:inline;'>"
            f"<input type='hidden' name='token' value='{token}'>"
            f"<input type='hidden' name='action' value='toggle'>"
            f"<input type='hidden' name='sponsor_id' value='{s['id']}'>"
            f"<input type='hidden' name='active' value='{toggle_next}'>"
            f"<button type='submit' style='background:#eee;color:#0b5d52;'>{toggle_label}</button>"
            f"</form></td>"
            f"<td style='padding:8px;color:#666;'>{s['created_at'][:10]}</td></tr>"
        )

    rows = "".join(sponsor_row(s) for s in list_sponsors()) or \
        "<tr><td colspan='6' style='padding:8px;color:#666;'>No sponsors yet.</td></tr>"

    created_banner = ""
    if created:
        created_banner = (
            f"<div style='background:#eafaf1;border:1px solid #2ecc71;border-radius:8px;padding:14px;margin-bottom:20px;'>"
            f"<strong>{created['name']}</strong> created for {', '.join(c.title() for c in created['counties'])} County. "
            f"Their page will now appear on every contract generated for a property in that county."
            f"</div>"
        )
    error_banner = f"<div style='color:#c0392b;margin-bottom:16px;'>{error}</div>" if error else ""

    return f"""<!DOCTYPE html>
<html><head><title>Title Sponsors — TxtAnOffer Admin</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>body{{font-family:-apple-system,sans-serif;max-width:1000px;margin:40px auto;padding:0 20px;color:#0b5d52;}}
input{{padding:8px 10px;border:1px solid #ccc;border-radius:6px;font-size:0.9rem;margin-right:8px;margin-bottom:8px;}}
button{{padding:8px 16px;border:none;border-radius:6px;background:#0b5d52;color:#fff;font-weight:600;cursor:pointer;}}
table{{width:100%;border-collapse:collapse;margin-top:20px;}}
th{{text-align:left;padding:8px;border-bottom:2px solid #eee;}}
td{{border-bottom:1px solid #eee;}}
a{{color:#0b5d52;}}
.hint{{color:#666;font-size:0.85rem;margin:-4px 0 12px;}}</style>
</head><body>
<h1>Title Sponsors</h1>
{created_banner}{error_banner}
<form method="POST">
  <input type="hidden" name="token" value="{token}">
  <input type="text" name="name" placeholder="Sponsor name (e.g. Lone Star Title Co.)" required>
  <input type="text" name="counties" placeholder="Counties, comma-separated (e.g. Harris, Fort Bend, Montgomery)" style="width:340px;" required>
  <br>
  <input type="text" name="tagline" placeholder="Tagline (optional)" style="width:260px;">
  <input type="text" name="contact_phone" placeholder="Phone (optional)">
  <input type="email" name="contact_email" placeholder="Email (optional)">
  <br>
  <button type="submit">Create</button>
</form>
<div class="hint">Selling the same county to two sponsors defeats the pitch — check the table below before creating a new one.</div>
<table>
<tr><th>Name</th><th>Counties</th><th>Contact</th><th>Status</th><th></th><th>Created</th></tr>
{rows}
</table>
</body></html>"""


@app.route("/broker/dashboard/<join_code>")
def broker_dashboard(join_code):
    """The roster view a managing broker is paying for: every agent linked
    to their brokerage (via SMS prefix or the signup form) and how much
    they're using TxtAnOffer, plus TC File Check's sitewide compliance
    numbers as context. join_code IS the access secret here -- there's no
    separate broker login yet, same pattern as /thread/<filename>."""
    brokerage = get_brokerage_by_code(join_code)
    if not brokerage:
        abort(404)

    agents = list_brokerage_agents(brokerage["id"])
    roster_emails = get_emails_for_phones([a["phone"] for a in agents])
    roster_scoped = bool(roster_emails)
    tc_summary = get_tc_check_summary(days=30, sender_emails=roster_emails) if roster_scoped else get_tc_check_summary(days=30)

    agent_rows = "".join(
        f"<tr><td style='padding:8px;'>{a['phone']}</td>"
        f"<td style='padding:8px;'>{a['offer_count']}</td>"
        f"<td style='padding:8px;color:#666;'>{(a['last_offer_at'] or 'never')[:10] if a['last_offer_at'] else 'never'}</td></tr>"
        for a in agents
    ) or "<tr><td colspan='3' style='padding:8px;color:#666;'>No agents linked yet. Agents join by texting "
    if not agents:
        agent_rows += f"<code>{brokerage['join_code']} [offer]</code> as their first message, or entering the code at signup.</td></tr>"

    total_offers = sum(a["offer_count"] for a in agents)
    tc_scope_note = (
        "Scoped to files checked by agents on your roster."
        if roster_scoped else
        "Not yet scoped to your roster specifically &mdash; no agent on your roster has an "
        "email on file yet, so this is every file checked on TxtAnOffer, shown as context "
        "for what the tool catches."
    )
    issue_rows = "".join(
        f"<tr><td style='padding:8px;'>{i['label']}</td><td style='padding:8px;'>{i['count']}</td>"
        f"<td style='padding:8px;'>{i['pct_of_recognized']}%</td></tr>"
        for i in tc_summary["issue_frequency"][:8]
    ) or "<tr><td colspan='3' style='padding:8px;color:#666;'>No data yet.</td></tr>"

    archive_q = (request.args.get("q") or "").strip()[:100]
    records = list_brokerage_records(brokerage["id"], archive_q)

    def _record_row(r):
        on_disk = os.path.isfile(os.path.join(OUTPUT_DIR, r["filename"]))
        if on_disk:
            expires, sig = sign_pdf_view_params(r["filename"])
            link = f"<a href='/offers/{escape(r['filename'])}?expires={expires}&sig={sig}' target='_blank' rel='noopener'>PDF</a>"
        else:
            link = "<span style='color:#8a9aa9;'>expired</span>"
        kind = "Offer (20-19)" if r["kind"] == "offer" else "Amendment (39-11)"
        return (f"<tr><td style='padding:8px;'>{(r['created_at'] or '')[:10]}</td>"
                f"<td style='padding:8px;'>{escape(r['address'] or '')}</td>"
                f"<td style='padding:8px;'>{kind}</td>"
                f"<td style='padding:8px;'>{escape(r['phone'] or '')}</td>"
                f"<td style='padding:8px;'>{link}</td></tr>")

    record_rows = "".join(_record_row(r) for r in records) or (
        f"<tr><td colspan='5' style='padding:8px;color:#666;'>No records match &ldquo;{escape(archive_q)}&rdquo;.</td></tr>"
        if archive_q else
        "<tr><td colspan='5' style='padding:8px;color:#666;'>No offers drafted by your roster yet.</td></tr>"
    )

    return f"""<!DOCTYPE html>
<html><head><title>{brokerage['name']} — TxtAnOffer</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
  * {{ box-sizing: border-box; }}
  body{{font-family:'Inter',-apple-system,sans-serif;max-width:900px;margin:40px auto;padding:0 20px;color:#0b5d52;background:#F5F5F7;}}
  h1{{font-size:1.6rem;margin-bottom:4px;}}
  .sub{{color:#5a6b7a;margin-bottom:28px;}}
  .card{{background:#fff;border:1px solid rgba(15,31,47,0.08);border-radius:14px;padding:22px;margin-bottom:24px;}}
  .card h2{{font-size:1.05rem;margin:0 0 14px;}}
  table{{width:100%;border-collapse:collapse;}}
  th{{text-align:left;padding:8px;border-bottom:2px solid #eee;font-size:0.8rem;color:#8a9aa9;text-transform:uppercase;}}
  td{{border-bottom:1px solid #eee;font-size:0.9rem;}}
  .stat-row{{display:flex;gap:24px;margin-bottom:24px;}}
  .stat{{background:#fff;border:1px solid rgba(15,31,47,0.08);border-radius:14px;padding:18px 22px;flex:1;}}
  .stat-num{{font-size:1.7rem;font-weight:800;}}
  .stat-label{{font-size:0.8rem;color:#8a9aa9;margin-top:2px;}}
</style>
</head><body>
<h1>{brokerage['name']}</h1>
<p class="sub">Roster &amp; compliance dashboard &middot; join code <code>{brokerage['join_code']}</code></p>

<div class="stat-row">
  <div class="stat"><div class="stat-num">{len(agents)}</div><div class="stat-label">Agents linked</div></div>
  <div class="stat"><div class="stat-num">{total_offers}</div><div class="stat-label">Offers drafted</div></div>
  <div class="stat"><div class="stat-num">{tc_summary['completion_rate']}%</div><div class="stat-label">{"Your roster's" if roster_scoped else "Sitewide"} file-complete rate (last 30d)</div></div>
</div>

<div class="card">
  <h2>Your roster</h2>
  <table>
    <tr><th>Phone</th><th>Offers drafted</th><th>Last offer</th></tr>
    {agent_rows}
  </table>
</div>

<div class="card">
  <h2>What TC File Check catches most {"for your roster" if roster_scoped else "(sitewide)"} (last 30 days)</h2>
  <p style="color:#5a6b7a;font-size:0.85rem;margin-top:-6px;margin-bottom:14px;">
    {tc_scope_note}
  </p>
  <table>
    <tr><th>Issue</th><th>Count</th><th>% of recognized files</th></tr>
    {issue_rows}
  </table>
</div>

<div class="card" style="background:#0a3f3a;color:#fff;">
  <h2 style="color:#fff;">Contract archive</h2>
  <p style="color:#c9dcd8;font-size:0.9rem;margin:6px 0 12px;">Forward or drop your agents&rsquo; executed contracts: each one is checked and kept 5 years, searchable by address.</p>
  <a href="/broker/login" style="display:inline-block;background:#f5c242;color:#0f1f2f;padding:9px 16px;border-radius:999px;font-weight:700;text-decoration:none;">Sign in to the archive &rarr;</a>
</div>

<div class="card" id="archive">
  <h2>Records archive</h2>
  <p style="color:#5a6b7a;font-size:0.85rem;margin-top:-6px;margin-bottom:14px;">
    Every offer and amendment your agents draft in TxtAnOffer is kept for 5 years while they're on your roster &mdash;
    to help with TREC's 4-year record-keeping rule (22 TAC &sect;535.2). These are the drafts generated here,
    not the final executed contracts, so keep your own copy of what was signed.
  </p>
  <form method="get" action="#archive" style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;">
    <input type="text" name="q" value="{escape(archive_q)}" placeholder="Search by address, e.g. 123 Main St" style="flex:1;min-width:200px;padding:9px 12px;border:1px solid #ddd;border-radius:9px;font:inherit;">
    <button type="submit" style="padding:9px 16px;border:0;border-radius:9px;background:#0b5d52;color:#fff;font:inherit;font-weight:600;cursor:pointer;">Search</button>
    <a href="/broker/dashboard/{brokerage['join_code']}/archive.zip" style="padding:9px 16px;border:1.5px solid #0b5d52;border-radius:9px;color:#0b5d52;font-weight:600;text-decoration:none;">Download all (ZIP)</a>
  </form>
  <div style="overflow-x:auto;">
  <table>
    <tr><th>Date</th><th>Address</th><th>Document</th><th>Agent</th><th>File</th></tr>
    {record_rows}
  </table>
  </div>
</div>

</body></html>"""


@app.route("/broker/dashboard/<join_code>/archive.zip")
def broker_archive_zip(join_code):
    """Export of the brokerage's whole records archive -- the "cancel
    anytime, take your records with you" promise. Same join_code-as-secret
    access as the dashboard itself."""
    import io
    import zipfile
    brokerage = get_brokerage_by_code(join_code)
    if not brokerage:
        abort(404)
    records = list_brokerage_records(brokerage["id"], limit=100000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        seen = set()
        for r in records:
            name = r["filename"]
            path = os.path.join(OUTPUT_DIR, name)
            if name in seen or "/" in name or ".." in name or not os.path.isfile(path):
                continue
            seen.add(name)
            zf.write(path, arcname=f"{(r['created_at'] or '')[:10]}_{name}")
    buf.seek(0)
    track_event("brokerage_archive_export", None, {"brokerage_id": brokerage["id"], "files": len(seen)})
    safe_name = re.sub(r"[^A-Za-z0-9]+", "-", brokerage["name"]).strip("-") or "brokerage"
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{safe_name}-records.zip"'})


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
        brokerage_code = request.form.get("brokerage_code", "").strip()
        if phone:
            account_ok = True
            try:
                if not get_user(phone):
                    create_user(phone)
            except Exception as e:
                account_ok = False
                print(f"[SIGNUP] create_user failed for {phone}: {e}")

            if account_ok and brokerage_code:
                brokerage = get_brokerage_by_code(brokerage_code)
                if brokerage:
                    link_user_to_brokerage(phone, brokerage["id"])
                    track_event("brokerage_linked", phone, {
                        "brokerage_id": brokerage["id"],
                        "brokerage_name": brokerage["name"],
                        "via": "signup_form",
                    })

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
                    'background:linear-gradient(135deg,var(--accent),#0a3a33);color:#fff;border-radius:var(--radius-sm);'
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
    --accent:#0b5d52;--accent-light:#16806e;--accent-dark:#0a3a33;--accent-tint:#E7F3F1;
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
  input:focus{{border-color:var(--accent);box-shadow:0 0 0 3px rgba(11,93,82,0.15);}}
  input::placeholder{{color:#b8c2ca;}}
  .consent-row{{
    display:flex;align-items:flex-start;gap:0.75rem;margin:1rem 0;padding:1rem;
    background:var(--accent-tint);border:1px solid rgba(11,93,82,0.2);border-radius:var(--radius-sm);
  }}
  .consent-row input[type=checkbox]{{margin-top:0.2rem;width:18px;height:18px;flex-shrink:0;accent-color:var(--accent);}}
  .consent-row label{{font-size:0.8rem;line-height:1.6;color:var(--text-muted);}}
  .consent-row a{{color:var(--accent-dark);text-decoration:underline;}}
  button{{
    width:100%;margin-top:0.75rem;
    background:linear-gradient(135deg,var(--accent),#0a3a33);color:#fff;border:none;
    padding:0.85rem;font-family:inherit;font-size:0.95rem;font-weight:600;
    border-radius:var(--radius-sm);cursor:pointer;transition:var(--transition);
  }}
  button:hover{{transform:translateY(-2px);box-shadow:0 8px 24px rgba(11,93,82,0.25);}}
  button:disabled{{opacity:0.4;cursor:not-allowed;transform:none;box-shadow:none;}}
  .success{{
    margin-top:1rem;padding:1rem;background:var(--accent-tint);
    border:1px solid rgba(11,93,82,0.2);border-radius:var(--radius-sm);
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
    <a href="/" class="nav-back"><span>&larr;</span><img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="width:auto;height:22px;border-radius:0;"></a>
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
        <input type="email" name="email" placeholder="you@example.com">
        <label class="field-label">Brokerage join code (optional)</label>
        <input type="text" name="brokerage_code" placeholder="Given to you by your managing broker" style="text-transform:uppercase;">
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
    --accent:#0b5d52;--accent-light:#16806e;--accent-dark:#0a3a33;--accent-tint:#E7F3F1;
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
  input:focus{{border-color:var(--accent);box-shadow:0 0 0 3px rgba(11,93,82,0.15);}}
  input::placeholder{{color:#b8c2ca;}}
  .sms-note{{font-size:0.8rem;color:var(--text-dim);margin:0.75rem 0 0;line-height:1.5;}}
  button{{
    width:100%;margin-top:1rem;
    background:linear-gradient(135deg,var(--accent),#0a3a33);color:#fff;border:none;
    padding:0.85rem;font-family:inherit;font-size:0.95rem;font-weight:600;
    border-radius:var(--radius-sm);cursor:pointer;transition:var(--transition);
  }}
  button:hover{{transform:translateY(-2px);box-shadow:0 8px 24px rgba(11,93,82,0.25);}}
  .msg{{margin-top:1rem;padding:0.85rem;border-radius:var(--radius-sm);font-size:0.9rem;text-align:center;}}
  .msg.success{{background:var(--accent-tint);border:1px solid rgba(11,93,82,0.2);color:var(--accent-dark);}}
  .msg.error{{background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.2);color:#dc2626;}}
  .msg a{{color:var(--accent-dark);}}
  .alt{{text-align:center;margin-top:1.25rem;font-size:0.85rem;color:var(--text-dim);}}
  .alt a{{color:var(--accent-dark);text-decoration:none;}}
  .alt a:hover{{text-decoration:underline;}}
</style>
</head>
<body>
<div class="wrap">
  <a href="/" class="nav-back"><span>&larr;</span><img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="width:auto;height:22px;border-radius:0;"></a>
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
    --accent: #0b5d52;
    --accent-light: #16806e;
    --accent-dark: #0a3a33;
    --accent-tint: #E7F3F1;
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
    <img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="height:26px;width:auto;display:block;">
  </a>
  <div class="nav-links" id="navLinks">
    <a href="/#how">How it works</a>
    <a href="/pricing">Pricing</a>
    <a href="/faq">FAQ</a>
    <a href="/login">Log In</a>
  </div>
  <a href="/tc-check" class="nav-cta">Try TC Check Free</a>
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
    <p><strong>Pricing:</strong> Plans start at $40.00 per month, billed monthly via Stripe. See <a href="/pricing" style="color:var(--accent-light);">pricing page</a> for current tiers.</p>
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
    <p><strong>Data retention:</strong> Generated PDFs on the free trial are stored temporarily and may be deleted after a reasonable period (currently 30 days); on a paid plan they are kept for 5 years from creation and shown in your dashboard. On the Brokerage plan, offers and amendments drafted by an agent linked to the brokerage are kept for 5 years from creation while that agent stays linked, and can be exported at any time from the brokerage dashboard. These are the drafts generated by the Service, not the executed contracts. Brokerage plan accounts may also store executed contracts in the contract archive, by forwarding them from an email linked to the brokerage or uploading them while signed in; those files are kept for 5 years from when they are added unless the brokerage deletes them sooner, are accessible only to the brokerage's signed-in users, and can be exported at any time. You are responsible for having the right to store any document you add. We retain account and billing records as required by law. <strong>Outside the Brokerage plan, this is shorter than the 4-year offer/contract/addenda retention period brokers are independently required to maintain under TREC Rule &sect;535.2.</strong> Our retention does not satisfy that obligation &mdash; you are responsible for downloading and separately retaining your own copy of every offer and amendment.</p>
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
    --accent: #0b5d52;
    --accent-light: #16806e;
    --accent-dark: #0a3a33;
    --accent-tint: #E7F3F1;
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
    <img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="height:26px;width:auto;display:block;">
  </a>
  <div class="nav-links" id="navLinks">
    <a href="/#how">How it works</a>
    <a href="/pricing">Pricing</a>
    <a href="/faq">FAQ</a>
    <a href="/login">Log In</a>
  </div>
  <a href="/tc-check" class="nav-cta">Try TC Check Free</a>
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
      <li><strong>Railway</strong> — Hosting provider; our app server and database run there</li>
      <li><strong>SendGrid</strong> — Inbound email: receives emails (and attached contracts) forwarded to tc@check.txtanoffer.com; also our backup outbound email provider</li>
      <li><strong>DocuSign</strong> — E-signature: when you send an offer for signature, the PDF and the signers&rsquo; names and emails</li>
      <li><strong>Your webhook</strong> — If you set up a webhook (e.g. Zapier), offer data is sent to the URL you choose</li>
      <li><strong>Resend</strong> — Outbound email: sends TC Check report emails (your email address, the property address and the findings), offer emails with the generated PDF, and account notifications</li>
    </ul>
    <p>We may disclose information if required by law, legal process, or to protect the rights and safety of our users or the public.</p>

    <h2>5. Data Retention</h2>
    <ul>
      <li>Generated PDFs: on the free trial, stored for download and deleted after 30 days; on a paid plan, kept 5 years in your dashboard</li>
      <li>Brokerage plan: offers and amendments drafted by an agent linked to a brokerage are kept for 5 years from creation while the agent stays linked, viewable and exportable by that brokerage</li>
      <li>Brokerage contract archive: executed contracts a brokerage forwards from a linked email or uploads while signed in are stored for 5 years from when they are added (or until the brokerage deletes them), visible only to that brokerage's signed-in users, and exportable anytime. Files checked outside the archive are still deleted after the check (bulk uploads: when the batch finishes).</li>
      <li>TC Check records: for each check we keep a record (your email if you gave one, and short issue codes &mdash; not the file, the property address or the form contents). Records older than 90 days are removed by an automatic cleanup that doesn&rsquo;t run on a fixed schedule, so some may last longer than 90 days. We also keep a per-browser count of free checks (a random browser ID and your email if you gave one) with no set expiry.</li>
      <li>TC Check bulk uploads: we keep your email, the number of files, and each file&rsquo;s original filename, whether it was recognized, its issue count and up to 10 issue messages, with no set expiry. Anyone with the batch results link can view them.</li>
      <li>Account data: retained while your account is active and for 90 days after cancellation</li>
      <li>Billing records: retained as required by applicable tax and accounting laws</li>
      <li>SMS logs: retained for 90 days for support and debugging purposes</li>
    </ul>
    <p><strong>Note for licensees:</strong> TREC Rule &sect;535.2 requires brokers to independently retain offers, contracts, and related addenda for at least 4 years from closing or termination. Our 30-day PDF retention does not satisfy that requirement. The Brokerage plan's contract archive can hold the executed contracts your brokerage forwards or uploads, plus the drafts generated here. It helps with that requirement, but it is not a guarantee of compliance &mdash; you remain responsible for keeping complete transaction records.</p>

    <h2>6. Data Security</h2>
    <p>We implement reasonable technical and organizational measures to protect your data:</p>
    <ul>
      <li><strong>Encryption in transit:</strong> the site is served over HTTPS</li>
      <li><strong>Infrastructure:</strong> hosted on Railway</li>
      <li><strong>Access:</strong> processing is automated; TxtAnOffer&rsquo;s founder can access stored data for support and debugging</li>
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
    --accent: #0b5d52;
    --accent-light: #16806e;
    --accent-dark: #0a3a33;
    --accent-tint: #E7F3F1;
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
    <img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="height:26px;width:auto;display:block;">
  </a>
  <div class="nav-links" id="navLinks">
    <a href="/#how">How it works</a>
    <a href="/pricing">Pricing</a>
    <a href="/faq">FAQ</a>
    <a href="/login">Log In</a>
  </div>
  <a href="/tc-check" class="nav-cta">Try TC Check Free</a>
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
    <p>Every generated PDF is a draft. You must review all fields &mdash; price, dates, address, percentages &mdash; before presenting to clients. The parser handles most phrasings, but you are the final check. Fields like buyer/seller names, earnest money, and financing terms are intentionally left blank for you to complete.</p>
  </div>

  <div class="faq-item">
    <h2>Can I amend an offer after I've already sent it?</h2>
    <p>Yes &mdash; text <strong>AMEND &lt;address&gt; price &lt;value&gt;</strong> or <strong>AMEND &lt;address&gt; close +&lt;days&gt;</strong> (e.g. <em>"AMEND 123 Main St price 730k"</em> or <em>"AMEND 123 Main St close +10"</em>) and you'll get back a filled TREC 39-11 Amendment for that contract. It's included on every plan, works the same way in the <a href="/demo" style="color:var(--accent-dark);">web demo</a>, and shows up nested under the original offer on your <strong>Dashboard</strong>. Only the price or closing-date field you asked to change is filled &mdash; everything else on the form is left blank for you to complete, same as the main contract.</p>
  </div>

  <div class="faq-item">
    <h2>Do you store my texts or offers?</h2>
    <p>On the free trial, generated PDFs are stored for download and deleted after 30 days. On the Agent plan they're kept 5 years in your dashboard. On the Brokerage plan, every offer and amendment your agents draft here, plus any executed contracts your brokerage forwards or uploads, is kept for 5 years in a searchable archive the broker can export. SMS logs are retained for 90 days for support and debugging. We do not sell or share your data. See our <a href="/privacy" style="color:var(--accent-dark);">Privacy Policy</a> for the full breakdown.</p>
    <p><strong>Important:</strong> TREC Rule &sect;535.2 requires brokers to independently retain records of offers, contracts, and related addenda for at least 4 years from closing or termination of the transaction. Our 30-day retention does not satisfy that requirement. The Brokerage archive helps, but it holds the drafts generated here, not the signed contracts &mdash; keep your own copy of every executed offer and amendment.</p>
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
    --accent: #0b5d52;
    --accent-light: #16806e;
    --accent-dark: #0a3a33;
    --accent-tint: #E7F3F1;
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
    <img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="height:26px;width:auto;display:block;">
  </a>
  <div class="nav-links" id="navLinks">
    <a href="/#how">How it works</a>
    <a href="/pricing">Pricing</a>
    <a href="/faq">FAQ</a>
    <a href="/login">Log In</a>
  </div>
  <a href="/tc-check" class="nav-cta">Try TC Check Free</a>
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


@app.route("/tc-hub")
def tc_hub():
    """Free reference hub for TX transaction coordinators -- not primarily
    SEO content, a reason to come back. Three sections built from things
    this app already knows for certain: the real TREC form data verified
    elsewhere in this codebase, the exact field list tc_audit.py checks
    (so the checklists match the product, not generic industry advice),
    and live production audit data for "Common TC Mistakes" (pulled fresh
    per request, not a hardcoded snapshot that goes stale). Deliberately
    does NOT include a "TC Rules & Responsibilities" section (what a TC
    can/cannot do, when it crosses into legal advice) -- that's UPL-
    adjacent territory that needs actual TREC-rules research before
    publishing anything under this brand, not generated from general
    knowledge. "2026 Changes" links out to the already-built
    /trec-changes page rather than duplicating it."""
    summary = get_tc_check_summary(days=30)
    if not PUBLIC_TC_STATS_ENABLED:
        summary = dict(summary, recognized=0, issue_frequency=[])
    mistake_rows = ""
    for issue in summary["issue_frequency"][:8]:
        mistake_rows += (
            '<div class="mistake-row">'
            f'<div class="mistake-label">{issue["label"]}</div>'
            '<div class="mistake-bar-track"><div class="mistake-bar" style="width:' + str(min(issue["pct_of_recognized"], 100)) + '%;"></div></div>'
            f'<div class="mistake-pct">{issue["pct_of_recognized"]}%</div>'
            '</div>'
        )
    mistakes_note = (
        f"Based on {summary['recognized']} real TREC 20-19 files run through TC Check in the last 30 days, "
        f"updated live &mdash; not a static list."
        if summary["recognized"] > 0 else
        "Not enough real-world files have been audited yet to publish an honest breakdown. "
        "TC Check is built to catch blank initials, the Effective Date, and the "
        "escrow, title and earnest-money fields -- run your own file at /tc-check to see where yours stands."
    )

    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Texas TC Hub — TxtAnOffer</title>
<meta name="description" content="Free reference material for Texas transaction coordinators: TREC form guides, file-review checklists, and the most common mistakes found across real audited closing files.">
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
    --accent: #0b5d52;
    --accent-light: #16806e;
    --accent-dark: #0a3a33;
    --accent-tint: #E7F3F1;
    --amber: #b45309;
    --amber-tint: #FEF3E7;
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
  .container {max-width:820px;margin:0 auto;padding:3rem 2rem 4rem;}
  .page-header {margin-bottom:0.5rem;}
  .page-header h1 {font-size:2.1rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.5rem;color:var(--text);}
  .page-header p {font-size:0.95rem;color:var(--text-muted);max-width:60ch;}
  .hub-nav {display:flex;flex-wrap:wrap;gap:0.6rem;margin:1.75rem 0 2.75rem;}
  .hub-nav a {font-size:0.8rem;font-weight:600;color:var(--accent-dark);background:var(--accent-tint);padding:0.45rem 0.9rem;border-radius:9999px;}
  .hub-nav a:hover {background:var(--accent);color:#fff;}
  .section {margin-bottom:3rem;}
  .section h2 {font-size:1.3rem;font-weight:800;letter-spacing:-0.02em;margin-bottom:0.4rem;}
  .section-sub {font-size:0.85rem;color:var(--text-muted);margin-bottom:1.5rem;max-width:65ch;}

  .form-grid {display:grid;grid-template-columns:1fr 1fr 1fr;gap:1rem;margin-bottom:1.25rem;}
  .form-card {background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-sm);padding:1.25rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);}
  .form-card .form-num {font-size:0.7rem;font-weight:700;letter-spacing:0.04em;text-transform:uppercase;color:var(--accent-dark);background:var(--accent-tint);display:inline-block;padding:0.2rem 0.55rem;border-radius:0.4rem;margin-bottom:0.6rem;}
  .form-card h3 {font-size:0.95rem;font-weight:700;margin-bottom:0.4rem;}
  .form-card p {font-size:0.8rem;color:var(--text-muted);margin-bottom:0.6rem;}
  .form-card .form-meta {font-size:0.72rem;color:var(--text-dim);margin-bottom:0.6rem;}
  .form-card a.form-link {font-size:0.78rem;font-weight:600;color:var(--accent-dark);text-decoration:underline;text-underline-offset:2px;}
  .also-covered {font-size:0.8rem;color:var(--text-muted);background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-sm);padding:0.9rem 1.1rem;}

  .checklist-grid {display:grid;grid-template-columns:1fr 1fr;gap:1rem;}
  .checklist-card {background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:1.5rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);}
  .checklist-card h3 {font-size:0.95rem;font-weight:700;margin-bottom:0.85rem;}
  .checklist-card ul {list-style:none;}
  .checklist-card li {font-size:0.82rem;color:var(--text-muted);padding:0.4rem 0 0.4rem 1.4rem;position:relative;border-bottom:1px solid var(--border);}
  .checklist-card li:last-child {border-bottom:none;}
  .checklist-card li::before {content:"";position:absolute;left:0;top:0.55rem;width:0.85rem;height:0.85rem;border:1.5px solid var(--text-dim);border-radius:0.25rem;}

  .mistake-row {display:flex;align-items:center;gap:0.75rem;padding:0.55rem 0;border-bottom:1px solid var(--border);}
  .mistake-row:last-child {border-bottom:none;}
  .mistake-label {font-size:0.82rem;color:var(--text);flex:0 0 44%;}
  .mistake-bar-track {flex:1;height:8px;background:var(--border);border-radius:9999px;overflow:hidden;}
  .mistake-bar {height:100%;background:var(--amber);border-radius:9999px;}
  .mistake-pct {font-size:0.78rem;font-weight:700;color:var(--amber);width:3.2rem;text-align:right;}
  .mistakes-card {background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:1.5rem 1.75rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);}
  .mistakes-note {font-size:0.76rem;color:var(--text-dim);margin-top:1rem;}

  .cta-card {background:linear-gradient(135deg, var(--accent-dark), var(--accent));color:#fff;border-radius:var(--radius);padding:2rem;text-align:center;}
  .cta-card .cta-kicker {font-size:0.72rem;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;opacity:0.75;margin-bottom:0.5rem;}
  .cta-card h3 {font-size:1.25rem;font-weight:800;margin-bottom:0.5rem;}
  .cta-card p {font-size:0.85rem;opacity:0.85;margin-bottom:1.25rem;}
  .cta-buttons {display:flex;gap:0.75rem;justify-content:center;flex-wrap:wrap;}
  .cta-buttons button, .cta-buttons a {font-size:0.85rem;font-weight:700;padding:0.7rem 1.4rem;border-radius:9999px;border:none;cursor:pointer;text-decoration:none;transition:var(--transition);}
  .cta-primary {background:#fff;color:var(--accent-dark);}
  .cta-primary:hover {transform:scale(1.04);}
  .cta-secondary {background:rgba(255,255,255,0.15);color:#fff;border:1px solid rgba(255,255,255,0.4) !important;}
  .cta-secondary:hover {background:rgba(255,255,255,0.25);}

  .changes-pointer {background:var(--accent-tint);border-radius:var(--radius-sm);padding:1.1rem 1.3rem;display:flex;justify-content:space-between;align-items:center;gap:1rem;flex-wrap:wrap;}
  .changes-pointer p {font-size:0.85rem;color:var(--accent-dark);}
  .changes-pointer a {font-size:0.82rem;font-weight:700;color:var(--accent-dark);text-decoration:underline;text-underline-offset:2px;white-space:nowrap;}

  .sources-box {background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:1.5rem 1.75rem;margin-top:2.5rem;}
  .sources-box h3 {font-size:0.95rem;font-weight:700;margin-bottom:0.6rem;}
  .sources-box p {font-size:0.82rem;color:var(--text-muted);margin-bottom:0.75rem;}
  .sources-box ul {padding-left:1.1rem;}
  .sources-box li {font-size:0.82rem;color:var(--text-muted);margin-bottom:0.4rem;}
  .sources-box a {color:var(--accent-dark);text-decoration:underline;text-underline-offset:2px;}
  .disclaimer-box {font-size:0.76rem;color:var(--text-dim);line-height:1.7;margin-top:1.25rem;padding:1.1rem 1.3rem;background:var(--amber-tint);border-radius:var(--radius-sm);}
  .foot {text-align:center;margin-top:2.5rem;font-size:0.8rem;color:var(--text-dim);}
  .foot a {color:var(--accent-dark);}
  .foot a:hover {text-decoration:underline;}

  @media(max-width:700px) {
    .container {padding:2rem 1rem 3rem;}
    .form-grid {grid-template-columns:1fr;}
    .checklist-grid {grid-template-columns:1fr;}
    .mistake-label {flex-basis:38%;font-size:0.76rem;}
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
    <img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="height:26px;width:auto;display:block;">
  </a>
  <div class="nav-links" id="navLinks">
    <a href="/#how">How it works</a>
    <a href="/tc-hub">TC Hub</a>
    <a href="/pricing">Pricing</a>
    <a href="/faq">FAQ</a>
    <a href="/login">Log In</a>
  </div>
  <a href="/tc-check" class="nav-cta">Try TC Check Free</a>
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
    <h1>Texas TC Hub</h1>
    <p>Practical transaction-coordination resources for Texas -- TREC form references, file-review checklists, and what actually goes wrong on real closing files.</p>
  </div>

  <div class="hub-nav">
    <a href="#forms">TREC Forms</a>
    <a href="#checklists">File-Review Checklists</a>
    <a href="#mistakes">Common TC Mistakes</a>
    <a href="#changes">2026 Changes</a>
    <a href="#checklist-download">Free Checklist</a>
  </div>

  <div class="section" id="forms">
    <h2>TREC Forms</h2>
    <p class="section-sub">The three forms most Texas resale transactions run through, with current TREC-published effective dates -- straight from trec.texas.gov, not a mirror.</p>
    <div class="form-grid">
      <div class="form-card">
        <div class="form-num">TREC 20-19</div>
        <h3>One to Four Family Residential Contract (Resale)</h3>
        <p>The main contract for a residential resale -- single-family homes, duplexes, triplexes, and four-plexes. Not for condos, new construction, or commercial.</p>
        <div class="form-meta">Effective """ + TREC_FORM_CURRENT_AS_OF + """</div>
        <a class="form-link" href="https://www.trec.texas.gov/forms/one-four-family-residential-contract-resale" target="_blank" rel="noopener">Official TREC source &rarr;</a>
      </div>
      <div class="form-card">
        <div class="form-num">TREC 39-11</div>
        <h3>Amendment to Contract</h3>
        <p>Changes or adds terms to a contract that's already been executed -- most often a price change or a closing-date extension.</p>
        <div class="form-meta">Effective """ + TREC_FORM_CURRENT_AS_OF + """</div>
        <a class="form-link" href="https://www.trec.texas.gov/forms/amendment" target="_blank" rel="noopener">Official TREC source &rarr;</a>
      </div>
      <div class="form-card">
        <div class="form-num">TREC 40-11</div>
        <h3>Third Party Financing Addendum</h3>
        <p>Required whenever any part of the purchase price is financed by a third party (not seller or buyer). Not attached at all on all-cash deals.</p>
        <div class="form-meta">Effective January 3, 2025</div>
        <a class="form-link" href="https://www.trec.texas.gov/forms/third-party-financing-addendum-0" target="_blank" rel="noopener">Official TREC source &rarr;</a>
      </div>
    </div>
    <div class="also-covered"><b>Also referenced on this site:</b> IABS 1-2 (Information About Brokerage Services), TREC 61-0 (Seller's Disclosure re: Groundwater/Surface Water Rights), and TREC 36-10 (HOA Addendum) -- see <a href="/trec-changes" style="color:var(--accent-dark);text-decoration:underline;">what changed &rarr;</a> for the full rundown of when each applies.</div>
  </div>

  <div class="section" id="checklists">
    <h2>File-Review Checklists</h2>
    <p class="section-sub">Organized by the point in the transaction where each check actually matters -- built from the exact fields TC Check verifies on a real file, not a generic industry list.</p>
    <div class="checklist-grid">
      <div class="checklist-card">
        <h3>Before Sending an Offer</h3>
        <ul>
          <li>Property address, city, and county are complete</li>
          <li>Buyer and Seller legal names are filled in</li>
          <li>Sales Price: cash portion (3A) + financing (3B) = total (3C)</li>
          <li>Earnest money and option fee amounts are filled in</li>
          <li>Escrow Agent name and Title Company are filled in</li>
          <li>Financing-type checkbox matches whether a 40-11 is attached</li>
          <li>Closing date reflects what was actually agreed</li>
          <li>HOA addendum attached if the property has mandatory membership</li>
        </ul>
      </div>
      <div class="checklist-card">
        <h3>After Execution</h3>
        <ul>
          <li>Effective Date is filled in -- the single most commonly missed field</li>
          <li>Buyer initials present on every page that requires them</li>
          <li>Seller initials present on every page that requires them</li>
          <li>All required signatures are present</li>
          <li>Any executed amendments are attached to the file</li>
        </ul>
      </div>
      <div class="checklist-card">
        <h3>Amendment Review (39-11)</h3>
        <ul>
          <li>Exactly one item box is checked -- not more, not zero</li>
          <li>If price changed, the new Sales Price still reconciles (A + B = C)</li>
          <li>Amendment correctly references the original contract's address</li>
          <li>Both parties initialed or signed the amendment itself</li>
        </ul>
      </div>
      <div class="checklist-card">
        <h3>Closing-File Review</h3>
        <ul>
          <li>Every field from the "Before Sending" list above is still filled</li>
          <li>40-11 loan amount matches Section 3B of the main contract</li>
          <li>Third Party Financing checkbox agrees between Section 3B and Section 22</li>
          <li>Initials present on every page, including the addendum</li>
          <li>No conflicting terms between the contract and any amendment</li>
        </ul>
      </div>
    </div>
  </div>

  <div class="section" id="mistakes">
    <h2>Common TC Mistakes</h2>
    <p class="section-sub">What actually shows up blank or inconsistent, ranked by how often TC Check has found it on real uploaded files.</p>
    <div class="mistakes-card">
      """ + (mistake_rows or '<p style="font-size:0.85rem;color:var(--text-muted);">No data yet -- check back after a few real files have been run through TC Check.</p>') + """
      <p class="mistakes-note">""" + mistakes_note + """</p>
    </div>
  </div>

  <div class="section" id="changes">
    <h2>2026 Changes</h2>
    <div class="changes-pointer">
      <p>What's currently required on the TREC 20-19 -- Paragraph 12B compensation language, the mandatory Water Disclosure, the HOA addendum -- and how it's tracked.</p>
      <a href="/trec-changes">See what changed &rarr;</a>
    </div>
  </div>

  <div class="section" id="checklist-download" style="margin-bottom:2rem;">
    <div class="cta-card">
      <div class="cta-kicker">Texas TC Checklist &mdash; Free</div>
      <h3>Before submitting a transaction, check these 12 items.</h3>
      <p>The combined "Before Sending" + "After Execution" checklist above, as a plain-text file you can keep on hand.</p>
      <div class="cta-buttons">
        <button class="cta-primary" onclick="downloadHubChecklist()">Download checklist</button>
        <a class="cta-secondary" href="/tc-check">Want the computer to check it instead? Try TC Check &rarr;</a>
      </div>
    </div>
  </div>

  <div class="sources-box">
    <h3>Verify this yourself</h3>
    <p>This page is a courtesy summary, not the authoritative text. TREC's own rules of conduct and licensing rules live in the Texas Administrative Code.</p>
    <ul>
      <li><a href="https://www.trec.texas.gov/agency-information/contracts" target="_blank" rel="noopener">TREC Promulgated Contract Forms</a> -- the Commission's own forms library.</li>
      <li><a href="https://www.trec.texas.gov/agency-information/rules-and-laws/trec-rules" target="_blank" rel="noopener">TREC Rules</a> -- the Commission's courtesy summary of Chapters 531, 533, 534, and 535.</li>
      <li><a href="https://texreg.sos.state.tx.us/public/readtac$ext.ViewTAC?tac_view=3&amp;ti=22&amp;pt=23" target="_blank" rel="noopener">Texas Administrative Code, Title 22, Part 23</a> -- the Secretary of State's official rule text.</li>
    </ul>
  </div>

  <div class="disclaimer-box">
    <strong>This page is educational information only -- it is not legal advice and is not a substitute for advice from a licensed Texas real estate attorney or your managing broker.</strong> TxtAnOffer is an independent, third-party tool and is NOT affiliated with, endorsed by, or partnered with the Texas Real Estate Commission (TREC). "TREC" and the form numbers referenced above are designations of the Texas Real Estate Commission. Checklists reflect what TC Check verifies on a file; they are not a complete substitute for your own professional judgment or your broker's review requirements. See our <a href="/terms" style="color:var(--text);text-decoration:underline;">Terms of Service</a> for the full disclaimer.
  </div>

  <p class="foot">Questions? Email <a href="mailto:support@txtanoffer.com">support@txtanoffer.com</a>.<br><a href="/">&larr; Back to home</a> &middot; <a href="/trec-changes">2026 Changes</a> &middot; <a href="/faq">FAQ</a> &middot; <a href="/terms">Terms</a></p>
</div>

<script>
function downloadHubChecklist(){
  var lines = [
    'Texas TC Checklist -- TxtAnOffer',
    'txtanoffer.com/tc-hub',
    '',
    'BEFORE SENDING AN OFFER',
    '[ ] Property address, city, and county are complete',
    '[ ] Buyer and Seller legal names are filled in',
    '[ ] Sales Price: cash portion (3A) + financing (3B) = total (3C)',
    '[ ] Earnest money and option fee amounts are filled in',
    '[ ] Escrow Agent name and Title Company are filled in',
    '[ ] Financing-type checkbox matches whether a 40-11 is attached',
    '[ ] Closing date reflects what was actually agreed',
    '[ ] HOA addendum attached if the property has mandatory membership',
    '',
    'AFTER EXECUTION',
    '[ ] Effective Date is filled in',
    '[ ] Buyer initials present on every page that requires them',
    '[ ] Seller initials present on every page that requires them',
    '[ ] All required signatures are present',
    '',
    'Educational reference only -- not legal advice. See txtanoffer.com/tc-hub for sourcing and disclaimer.'
  ];
  var blob = new Blob([lines.join('\\n')], {type: 'text/plain'});
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url;
  a.download = 'texas-tc-checklist.txt';
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
</script>
</body>
</html>"""
    return html


@app.route("/about")
def about():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>About — TxtAnOffer</title>
<meta name="description" content="TxtAnOffer started as a faster way to text in an offer. It became TC File Check &mdash; a free tool that catches what's missing in a TREC 20-19 before title does. Here's the story.">
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
    --accent: #0b5d52;
    --accent-light: #16806e;
    --accent-dark: #0a3a33;
    --accent-tint: #E7F3F1;
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
    <img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="height:26px;width:auto;display:block;">
  </a>
  <div class="nav-links" id="navLinks">
    <a href="/#how">How it works</a>
    <a href="/pricing">Pricing</a>
    <a href="/faq">FAQ</a>
    <a href="/login">Log In</a>
  </div>
  <a href="/tc-check" class="nav-cta">Try TC Check Free</a>
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
  <h1>Built After Listening to Texas Real Estate</h1>
  <div class="kicker">The story behind TxtAnOffer</div>

  <div class="about-body">
    <p>Hi, I'm <strong>Phanel Jean Baptiste</strong>, the founder of TxtAnOffer.</p>

    <p>I'm not a real estate agent &mdash; I'm a software builder with a passion for solving real problems with simple tools. TxtAnOffer started after a conversation with a Texas REALTOR who walked me through what a bad day actually looks like: standing in a driveway or sitting in a parking lot, laptop open, manually filling 40+ fields on a TREC 20-19 while a buyer waits, because in Texas real estate the agent who gets their offer in first often gets the house.</p>

    <p>That time costs deals. So I built a way to skip it. TxtAnOffer turns what used to take a laptop into a text message and about 10 seconds.</p>

    <p><strong>That solved the drafting. It didn't solve what happens next.</strong> The more I dug into how Texas files actually close, the clearer it got: the real risk in a Texas contract isn't how fast it gets written &mdash; it's what's still blank or mismatched by the time it reaches title. A missing initial. An Effective Date nobody filled in. A 40-11 that disagrees with the contract it's attached to. That's not a speed problem, it's a review problem, and it can happen on any file &mdash; not just the ones typed from a phone.</p>

    <p>So TxtAnOffer became two things. <strong>Create</strong> an offer by text in seconds &mdash; still here, free to start. And <strong>Check</strong> any TREC 20-19 for exactly what's missing before title finds it &mdash; free, no login, just drop it on the site. Brokerages and TC teams that want every file checked automatically, across their whole roster, get the Brokerage Dashboard.</p>

    <h2>Why It's Built This Way</h2>
    <p>Every feature is built around a specific way a Texas file goes wrong &mdash; not because a spec sheet said a contract tool should have it:</p>
    <ul>
      <li><strong>Field-by-field verification</strong> because a checklist that's just guessing from a form's field names misses the exact mistakes that get a file kicked back</li>
      <li><strong>No login to check a file</strong> because a TC who needs an answer in the next two minutes shouldn't have to create an account first</li>
      <li><strong>SMS-first offer drafting</strong> because most agents I talked to are in the field on their phone, not at a desk</li>
      <li><strong>Draft warnings</strong> because nothing should go out until the licensed agent sending it has actually reviewed it</li>
    </ul>

    <h2>The Mission</h2>
    <p>Give Texas real estate professionals &mdash; agents, TCs, and the brokers who manage them &mdash; a faster way to create a contract and a sharper way to catch what's wrong with one, before it costs a deal or a closing.</p>
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


# --- SEO guides -----------------------------------------------------------
# Teaching pages for what Texas TCs and brokers actually search. Every TREC
# fact here comes from tc_audit.py's rect-verified field map (page/section
# numbers checked against the printed form) or from TREC's own FAQ -- keep it
# that way. Teach first, link to the tool second. Added 2026-10-06.

_GUIDES = {
    "trec-20-19-checklist": {
        "title": "TREC 20-19 Checklist: What to Check Before Sending to Title",
        "description": "A field-by-field checklist for the TREC 20-19 One to Four Family Residential Contract: the blanks and mismatches that most often get a Texas file kicked back by title.",
        "kicker": "For transaction coordinators",
        "h1": "TREC 20-19 checklist: what to check before you send the file to title",
        "lede": "Most kickbacks from title aren't legal problems. They're blanks and mismatches that were easy to fix before the file left your desk. Here's the order to check them in.",
        "body": """
<h2>1. Property and parties (Sections 1 and 2A)</h2>
<ul>
  <li><strong>Property address, city and county (Section 2A).</strong> County is the one most often left blank, and title needs it to open the file.</li>
  <li><strong>Buyer and seller legal names (Section 1).</strong> Match them to how the parties will sign at closing, not a nickname or a partial name.</li>
</ul>

<h2>2. Money and who holds it (Sections 5A and 6A)</h2>
<ul>
  <li><strong>Escrow agent name (Section 5A).</strong> If it's blank, nobody knows where the earnest money goes.</li>
  <li><strong>Earnest money and option fee amounts (Section 5A).</strong> Both should be filled in, even when the option fee is small.</li>
  <li><strong>Title company (Section 6A).</strong> Should match the escrow agent in most deals. If it doesn't, confirm that's intentional.</li>
</ul>

<h2>3. Financing: contract vs. 40-11 addendum</h2>
<ul>
  <li><strong>Loan amount.</strong> The third-party financing amount in Section 3B must match the principal amount on the TREC 40-11 Third Party Financing Addendum. A one-digit typo here is a common kickback.</li>
  <li><strong>The financing checkboxes.</strong> If a 40-11 is attached, the Third Party Financing Addendum box should be checked in <em>both</em> Section 3B and the Section 22 addenda list. If it's an all-cash deal with no 40-11, neither box should be checked.</li>
</ul>

<h2>4. Initials on every page</h2>
<p>The 20-19 has an "Initialed for identification by Buyer ___ and Seller ___" line at the bottom of pages 1, 4, 5, 6, 8 and 9 of 12, and the 40-11 has one on its first page. Check all four slots on each page (two buyers, two sellers). A single missing initial is easy to miss scrolling through, and it's a routine reason a file bounces back.</p>

<h2>5. The Effective Date (page 10 of 12)</h2>
<p>The line reads "EXECUTED the ___ day of ___, 20__ (Effective Date)," with the instruction "BROKER: FILL IN THE DATE OF FINAL ACCEPTANCE." Deadlines in the contract count from it, so a blank here creates confusion about every date that follows. <a href="/guides/trec-20-19-effective-date">More on why the Effective Date matters &rarr;</a></p>

<h2>6. Amendments (TREC 39-11)</h2>
<p>If there's an amendment, confirm its sales price matches what the deal actually is now, and that its property address matches the contract. An amendment with the wrong address usually means the wrong file got attached.</p>

<h2>Make sure you're on the current form</h2>
<p>The current TREC 20-19 has been mandatory since July 1, 2026. An old template saved on someone's computer is a quiet source of problems. <a href="/trec-changes">See what's on the current version &rarr;</a></p>
""",
    },
    "trec-20-19-effective-date": {
        "title": "TREC 20-19 Effective Date Left Blank: Why It Matters",
        "description": "Where the Effective Date goes on the TREC 20-19, who fills it in, and why a blank Effective Date causes problems for every deadline that counts from it.",
        "kicker": "For transaction coordinators",
        "h1": "Effective Date left blank on a TREC 20-19: why it matters and how to catch it",
        "lede": "It's one line near the end of a 12-page contract, it's filled in last, and it's the date the rest of the contract counts from. That's exactly why it gets missed.",
        "body": """
<h2>Where it is</h2>
<p>On the TREC 20-19, the Effective Date is on <strong>page 10 of 12</strong>: "EXECUTED the ___ day of ___, 20__ (Effective Date)." The form itself says who fills it in: "BROKER: FILL IN THE DATE OF FINAL ACCEPTANCE."</p>

<h2>Why it gets missed</h2>
<ul>
  <li>It can't be filled in when the offer is written, because final acceptance hasn't happened yet.</li>
  <li>Once the last party signs, everyone's attention moves to the next step: earnest money, the option period, the lender.</li>
  <li>It sits near the signature block on page 10, far from the terms people actually review.</li>
</ul>

<h2>Why it matters</h2>
<p>The contract's timelines run from the Effective Date. Deadlines such as delivering earnest money and the option fee, and the length of the option period in Paragraph 5, are counted in days after it. With the date blank, everyone has to guess when those clocks started. Title and lenders will ask for it, and the file stalls until someone fills it in.</p>

<h2>How to catch it every time</h2>
<ol>
  <li>Make "Effective Date filled in?" the first thing you check when an executed contract comes in, before anything else.</li>
  <li>Confirm it matches the date of final acceptance, not the date the offer was written.</li>
  <li>Calendar the Paragraph 5 deadlines from that date right away, while you're looking at it.</li>
</ol>

<p class="note">This is general information about the form, not legal advice. For questions about a specific deal, ask the broker or a Texas real estate attorney.</p>

<p><a href="/guides/trec-20-19-checklist">See the full TREC 20-19 pre-title checklist &rarr;</a></p>
""",
    },
    "broker-record-retention-texas": {
        "title": "How Long Must Texas Brokers Keep Transaction Records? (22 TAC 535.2)",
        "description": "Texas brokers must keep transaction records at least four years from closing or termination under TREC rule 22 TAC §535.2(h). What that means in practice and how to keep up with it.",
        "kicker": "For managing brokers",
        "h1": "How long do Texas brokers have to keep transaction records?",
        "lede": "At least four years from the date of closing or termination of the contract. That's the TREC rule. The hard part isn't knowing it, it's being able to find a specific file three years later.",
        "body": """
<h2>The rule</h2>
<p>TREC requires a broker to keep transaction records for <strong>at least four years from the date of closing or termination</strong> of a contract, in a format that can be made readily available to the Commission. The rule is in the Texas Administrative Code, Title 22, <strong>§535.2(h)</strong>, and TREC summarizes it in its <a href="https://www.trec.texas.gov/node/413" rel="noopener" target="_blank">license holder FAQ</a>.</p>

<h2>Why it's harder than it sounds</h2>
<ul>
  <li><strong>The clock starts at closing, not at the offer.</strong> A contract written in January that closes in March is kept until at least March four years later.</li>
  <li><strong>Records live with agents.</strong> Offers and amendments often sit in agents' email, phones and personal drives. When an agent leaves, so do the files.</li>
  <li><strong>"Readily available" means findable.</strong> A file you can't locate by address when someone asks isn't much help.</li>
</ul>

<h2>A practical setup</h2>
<ol>
  <li>Keep one place per brokerage where every executed contract, addendum and amendment lands, searchable by property address.</li>
  <li>File by closing (or termination) date so you know when the four years end.</li>
  <li>When an agent leaves, make sure their transaction files stay with the brokerage.</li>
  <li>Spot-check a few closed files each quarter: can you pull the full file in under a minute?</li>
</ol>

<h2>Where TxtAnOffer fits</h2>
<p>On the Brokerage plan, you can forward executed contracts to tc@check.txtanoffer.com from your brokerage email, or drop them into your archive. Each one is checked for what title kicks back and kept for 5 years, searchable by address and exportable anytime. Offers your agents text in to TxtAnOffer are archived too. It <em>helps</em> with this rule, but you remain responsible for keeping complete records.</p>

<p class="note">This is general information, not legal advice. Confirm your brokerage's obligations against the current rule text or with a Texas real estate attorney. TxtAnOffer is not affiliated with TREC.</p>
""",
    },
    "40-11-loan-amount-mismatch": {
        "title": "TREC 40-11 Loan Amount Doesn't Match the Contract (Section 3B)",
        "description": "Why the loan amount on the TREC 40-11 Third Party Financing Addendum must match Section 3B of the TREC 20-19, and the checkbox mistakes that go with it.",
        "kicker": "For transaction coordinators",
        "h1": "40-11 loan amount doesn't match the contract: how to catch it before title does",
        "lede": "When a deal is financed, the loan amount is written in two places: Section 3B of the TREC 20-19 and the TREC 40-11 Third Party Financing Addendum. If the two disagree, the file stops until someone fixes it.",
        "body": """
<h2>The two numbers that must match</h2>
<ul>
  <li><strong>TREC 20-19, Section 3B:</strong> the sum of all financing described in the attached Third Party Financing Addendum.</li>
  <li><strong>TREC 40-11:</strong> the principal amount of the loan the buyer is applying for.</li>
</ul>
<p>For a deal with a single loan, those should be the same dollar amount. A typo, a number carried over from an earlier draft, or a price change that only got updated in one place is all it takes. Example: Section 3B says $360,000 and the 40-11 says $36,000 on a 123 Main St purchase. Easy to type, easy to miss when you're skimming.</p>

<h2>The checkbox half of the same problem</h2>
<p>The 20-19 also has to say that a 40-11 is attached, in two places:</p>
<ul>
  <li>The <strong>Third Party Financing Addendum</strong> box in <strong>Section 3B</strong></li>
  <li>The same addendum in the <strong>Section 22</strong> list of attached addenda</li>
</ul>
<p>If a 40-11 is attached, both should be checked. If the deal is all cash and there's no 40-11, neither should be. A checked box with no addendum, or an addendum with an unchecked box, both cause questions.</p>

<h2>Don't forget the 40-11's own initials</h2>
<p>The 40-11 has its own "Initialed for identification by Buyer ___ and Seller ___" line on its first page. It's a separate document, so it's easy to check every page of the 20-19 and forget this one.</p>

<h2>A 30-second check</h2>
<ol>
  <li>Put Section 3B and the 40-11 principal amount side by side. Same number?</li>
  <li>Section 3B box and Section 22 box: both checked if there's a 40-11, neither if there isn't.</li>
  <li>40-11 initials: all four slots filled.</li>
</ol>
<p>If the contract is already signed and the numbers disagree, fixing it usually means getting both parties to sign a correction. Catching it before it goes out is much cheaper. When the sales price changes later, remember the financing amount often needs to change with it.</p>

<p class="note">General information about the forms, not legal or lending advice.</p>
<p><a href="/guides/trec-20-19-checklist">See the full TREC 20-19 pre-title checklist &rarr;</a></p>
""",
    },
    "trec-20-19-initials": {
        "title": "TREC 20-19 Initials: Which Pages Need Buyer and Seller Initials",
        "description": "The TREC 20-19 has an initials line on pages 1, 4, 5, 6, 8 and 9 of 12, plus one on the 40-11 addendum. Here's where they are and how to check them fast.",
        "kicker": "For transaction coordinators",
        "h1": "TREC 20-19 initials: which pages need them, and how to check fast",
        "lede": "A missing initial is one of the most routine reasons a Texas file bounces back. It's not hard to fix. It's just easy to miss when you're scrolling 12 pages.",
        "body": """
<h2>Where the initials lines are</h2>
<p>At the bottom of these pages of the TREC 20-19 (the page numbers are printed in each page's footer as "Page X of 12"):</p>
<ul>
  <li>Page 1 of 12</li>
  <li>Page 4 of 12</li>
  <li>Page 5 of 12</li>
  <li>Page 6 of 12</li>
  <li>Page 8 of 12</li>
  <li>Page 9 of 12</li>
</ul>
<p>Each line reads "Initialed for identification by Buyer ___ ___ and Seller ___ ___", which gives <strong>four slots per page</strong>: two for buyers and two for sellers. If a TREC 40-11 Third Party Financing Addendum is attached, it has the same line on its first page.</p>

<h2>Why they get missed</h2>
<ul>
  <li>The line is small and sits in the footer, where eyes skip.</li>
  <li>E-signature tools sometimes place initials on some pages but not others, especially when a template was built on an older version of the form.</li>
  <li>With two buyers, it's common for one buyer to initial and the other to be skipped.</li>
</ul>

<h2>How to check fast</h2>
<ol>
  <li>Jump straight to the six page numbers above instead of reading page by page.</li>
  <li>Count the slots on each one: one per buyer and one per seller who's party to the contract.</li>
  <li>Then check the 40-11's first page if it's attached.</li>
  <li>If your e-sign template is older than the current form (mandatory since July 1, 2026), rebuild it so the initials fields land on the right pages.</li>
</ol>

<p class="note">General information about the form, not legal advice.</p>
<p><a href="/guides/trec-20-19-checklist">See the full TREC 20-19 pre-title checklist &rarr;</a></p>
""",
    },
    "trec-39-11-amendment-mismatch": {
        "title": "TREC 39-11 Amendment: Checking Price and Address Against the Contract",
        "description": "Before a TREC 39-11 Amendment goes into the file, check that its sales price and property address match the contract it amends. Here's what to look for.",
        "kicker": "For transaction coordinators",
        "h1": "TREC 39-11 amendment: check the price and address against the contract",
        "lede": "An amendment changes the deal, so it has to clearly belong to the right contract and say the right numbers. Two quick comparisons catch most of the problems.",
        "body": """
<h2>1. Does it belong to this contract?</h2>
<p>Compare the property address on the 39-11 with the address in Section 2A of the TREC 20-19. A different address almost always means the wrong amendment got attached, which is a bigger problem than a typo: the change you think is in the file isn't.</p>

<h2>2. Is the sales price right?</h2>
<p>If the amendment changes the sales price, check that the new total is what the parties actually agreed to, and that the rest of the file reflects it. An amendment used only to change the closing date can leave the price section blank, and that's fine. A blank price section on a closing-date amendment isn't a mistake.</p>

<h2>3. What else usually moves with a price change</h2>
<ul>
  <li><strong>Financing.</strong> If there's a TREC 40-11, the loan amount may need to change too. Check it against the new price. <a href="/guides/40-11-loan-amount-mismatch">How the 40-11 and Section 3B have to match &rarr;</a></li>
  <li><strong>Signatures.</strong> An amendment isn't effective until the parties sign it. Make sure the executed version is what's in the file, not the draft.</li>
</ul>

<h2>A quick routine for every amendment</h2>
<ol>
  <li>Address on the 39-11 = address on the 20-19.</li>
  <li>If the price changed: the new total matches what was agreed, and financing was updated to match.</li>
  <li>Executed copy, not the draft.</li>
  <li>Calendar any new dates right away.</li>
</ol>

<p class="note">General information about the forms, not legal advice. For questions about a specific change, ask the broker or a Texas real estate attorney.</p>
<p><a href="/guides/trec-20-19-checklist">See the full TREC 20-19 pre-title checklist &rarr;</a></p>
""",
    },
    "trec-deadline-calculator": {
        "title": "TREC 20-19 Deadline Calculator: Option Period and Earnest Money",
        "description": "Free calculator for Texas TREC 20-19 deadlines: option period end (5:00 p.m., no weekend extension) and earnest money and option fee due dates with the Saturday, Sunday and Legal Holiday rule.",
        "kicker": "Free tool for TCs and agents",
        "h1": "TREC 20-19 deadline calculator",
        "lede": "Enter the Effective Date and the days in the contract. You get the option period end and the earnest money and option fee due date, with the weekend and holiday rule applied the way the form says.",
        "body": """
<style>
  .calc { background:#fff; border:1px solid var(--border); border-radius:1rem; padding:1.5rem; margin:1.5rem 0 1rem; }
  .calc-row { display:grid; grid-template-columns:repeat(3, 1fr); gap:1rem; }
  .calc label { display:block; font-size:0.8rem; font-weight:600; color:var(--text); margin-bottom:0.35rem; }
  .calc input { width:100%; padding:0.7rem 0.8rem; border:1px solid #d5dbe0; border-radius:0.6rem; font:inherit; font-size:1rem; background:#fff; }
  .calc .hint { font-size:0.75rem; color:var(--dim); margin-top:0.3rem; }
  .results { margin-top:1.25rem; display:grid; gap:0.75rem; }
  .res { border-radius:0.8rem; padding:1rem 1.1rem; background:var(--tint); }
  .res .lbl { font-size:0.75rem; font-weight:700; text-transform:uppercase; letter-spacing:0.05em; color:var(--accent-dark); }
  .res .val { font-size:1.35rem; font-weight:800; letter-spacing:-0.02em; margin:0.15rem 0; color:var(--text); }
  .res .why { font-size:0.85rem; color:var(--muted); }
  .res.warn { background:#FFF6E5; }
  .res.warn .lbl { color:#8a5a00; }
  @media (max-width:600px) { .calc-row { grid-template-columns:1fr; } }
</style>
<div class="calc">
  <div class="calc-row">
    <div><label for="ed">Effective Date</label><input type="date" id="ed"><div class="hint">Date of final acceptance (page 10)</div></div>
    <div><label for="opt">Option period (days)</label><input type="number" id="opt" min="0" max="60" placeholder="e.g. 7"><div class="hint">Paragraph 5B</div></div>
    <div><label for="add">Extra earnest money (days)</label><input type="number" id="add" min="0" max="120" placeholder="optional"><div class="hint">Paragraph 5A(1), if used</div></div>
  </div>
  <div class="results" id="results" aria-live="polite"><div class="res"><div class="why">Pick an Effective Date to see the deadlines.</div></div></div>
</div>

<h2>How these deadlines are counted</h2>
<ul>
  <li><strong>The Effective Date is day 0.</strong> Day 1 is the next calendar day. The contract counts calendar days, not business days.</li>
  <li><strong>Earnest money and option fee: within 3 days after the Effective Date</strong> (Paragraph 5A). If that last day falls on a Saturday, Sunday or Legal Holiday, it moves to the end of the next day that isn't one (Paragraph 5A(2)). Additional earnest money follows the same rule.</li>
  <li><strong>Option period: ends at 5:00 p.m. local time where the property is located</strong> on the last day (Paragraph 5B). There is <strong>no</strong> weekend or holiday extension for the option period. If it ends on a Sunday, it ends on that Sunday.</li>
  <li><strong>"Legal Holiday"</strong> is defined by the form as the holidays in Texas Government Code &sect;662.003(a), plus June 19 and the Friday after Thanksgiving (&sect;662.003(b)(4) and (6)). Other state holidays, such as December 24 and 26, aren't included. The calculator uses the statutory dates and doesn't apply "observed" Mondays.</li>
</ul>
<p class="note">This calculator applies the standard TREC 20-19 language. Special provisions, addenda or amendments can change deadlines. Check the actual contract, and when a deadline is close, confirm it with the broker or a Texas real estate attorney. Not legal advice. Sources: TREC 20-19 Paragraph 5; Texas Government Code &sect;662.003; <a href="https://trerc.tamu.edu/article/Option-Period-Basics-2360/" target="_blank" rel="noopener">Texas A&amp;M Real Estate Center, &ldquo;Option Period Basics&rdquo;</a>.</p>

<script src="/static/trec-deadlines.js?v=1"></script>
<script>
(function(){
  var T = window.TrecDeadlines, ed = document.getElementById('ed'), opt = document.getElementById('opt'), add = document.getElementById('add'), out = document.getElementById('results');
  function fmt(iso){ var p = iso.split('-').map(Number); return new Date(Date.UTC(p[0], p[1]-1, p[2])).toLocaleDateString('en-US', {weekday:'long', month:'long', day:'numeric', year:'numeric', timeZone:'UTC'}); }
  function esc(t){ return String(t).replace(/[&<>]/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c]; }); }
  function card(cls, lbl, val, why){ return '<div class="res' + (cls ? ' ' + cls : '') + '"><div class="lbl">' + lbl + '</div><div class="val">' + val + '</div><div class="why">' + why + '</div></div>'; }
  function money(label, r){
    if (!r.extended) return card('', label, esc(fmt(r.date)), 'End of day. Not a Saturday, Sunday or Legal Holiday, so no extension.');
    var why = 'Would have been ' + esc(fmt(r.original)) + ', but ' + r.skipped.map(function(s){ return esc(fmt(s.date).split(',')[0]) + ' is ' + (s.reason === 'Saturday' || s.reason === 'Sunday' ? 'a weekend day' : esc(s.reason)); }).join('; ') + '. Extended to the end of the next day that isn\u2019t (Paragraph 5A(2)).';
    return card('', label, esc(fmt(r.date)), why);
  }
  function run(){
    if (!ed.value) { out.innerHTML = '<div class="res"><div class="why">Pick an Effective Date to see the deadlines.</div></div>'; return; }
    var e = T.parse(ed.value), html = money('Earnest money + option fee due', T.earnestDeadline(e, 3));
    var od = parseInt(opt.value, 10);
    if (od >= 0 && od <= 60) {
      var o = T.optionDeadline(e, od);
      html += o.fallsOn
        ? card('warn', 'Option period ends', esc(fmt(o.date)) + ', 5:00 p.m.', 'That day is ' + (o.fallsOn === 'Saturday' || o.fallsOn === 'Sunday' ? 'a ' + o.fallsOn : esc(o.fallsOn)) + ', but the option period is <strong>not</strong> extended. Notice must be given by 5:00 p.m. local time that day (Paragraph 5B).')
        : card('', 'Option period ends', esc(fmt(o.date)) + ', 5:00 p.m.', 'Local time where the property is located (Paragraph 5B).');
    }
    var ad = parseInt(add.value, 10);
    if (ad > 0 && ad <= 120) html += money('Additional earnest money due', T.earnestDeadline(e, ad));
    out.innerHTML = html;
  }
  [ed, opt, add].forEach(function(el){ el.addEventListener('input', run); el.addEventListener('change', run); });
})();
</script>
""",
    },
}


def _guide_page(slug, g, body_html):
    cta_brokers = g["kicker"] == "For managing brokers"
    cta = (
        """<div class="cta"><strong>See what your agents leave blank.</strong> Run your last 20 closed TREC 20-19s through TC Check, free.
  <a class="btn" href="/brokers?src=seo_""" + slug + """" data-evt="guide_brokers_cta">Free 20-file audit &rarr;</a></div>"""
        if cta_brokers else
        """<div class="cta"><strong>Check a file in seconds.</strong> Drop a filled TREC 20-19 and get back exactly which of these are blank or mismatched. Free, no signup, and the file isn't kept.
  <a class="btn" href="/tc-check?src=seo_""" + slug + """" data-evt="guide_check_cta">Check a file free &rarr;</a></div>"""
    )
    others = "".join(
        f'<li><a href="/guides/{s}">{escape(o["h1"])}</a></li>' for s, o in _GUIDES.items() if s != slug
    ) if slug != "index" else ""
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>""" + escape(g["title"]) + """ — TxtAnOffer</title>
<meta name="description" content=\"""" + escape(g["description"]) + """\">
<link rel="canonical" href="https://txtanoffer.com/guides/""" + slug + """">
<meta property="og:title" content=\"""" + escape(g["title"]) + """\">
<meta property="og:description" content=\"""" + escape(g["description"]) + """\">
<meta property="og:type" content="article">
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  :root { --bg:#F5F5F7; --card:#fff; --border:rgba(15,31,47,0.08); --text:#0f1f2f; --muted:#5a6b7a; --dim:#8a9aa9; --accent:#0b5d52; --accent-light:#16806e; --accent-dark:#0a3a33; --tint:#E7F3F1; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif; background:var(--bg); color:var(--text); line-height:1.65; -webkit-font-smoothing:antialiased; }
  a { color:var(--accent-dark); }
  .nav { display:flex; align-items:center; justify-content:space-between; padding:1rem 2rem; position:sticky; top:0; background:rgba(255,255,255,0.9); backdrop-filter:blur(20px); -webkit-backdrop-filter:blur(20px); border-bottom:1px solid var(--border); z-index:10; }
  .nav-cta { background:var(--accent); color:#fff; padding:0.55rem 1.3rem; border-radius:9999px; font-size:0.875rem; font-weight:600; text-decoration:none; white-space:nowrap; }
  .wrap { max-width:720px; margin:0 auto; padding:3rem 2rem 4rem; }
  .crumbs { font-size:0.8rem; color:var(--dim); margin-bottom:1rem; }
  .crumbs a { color:var(--dim); }
  .kicker { font-size:0.75rem; font-weight:700; color:var(--accent-dark); text-transform:uppercase; letter-spacing:0.06em; margin-bottom:0.6rem; }
  h1 { font-size:2.1rem; font-weight:800; letter-spacing:-0.03em; line-height:1.15; margin-bottom:1rem; text-wrap:balance; }
  .lede { font-size:1.08rem; color:var(--muted); margin-bottom:0.75rem; }
  .byline { font-size:0.8rem; color:var(--dim); margin-bottom:2rem; }
  h2 { font-size:1.25rem; font-weight:700; letter-spacing:-0.02em; margin:2.2rem 0 0.8rem; text-wrap:balance; }
  p { margin-bottom:1rem; color:#2c3d4d; }
  ul, ol { margin:0 0 1rem 1.3rem; color:#2c3d4d; }
  li { margin-bottom:0.55rem; }
  .note { font-size:0.85rem; color:var(--dim); border-left:3px solid var(--border); padding-left:0.9rem; }
  .cta { background:var(--tint); border-radius:1rem; padding:1.4rem 1.5rem; margin:2.5rem 0 1rem; color:var(--muted); font-size:0.95rem; }
  .cta strong { display:block; color:var(--text); font-size:1.05rem; margin-bottom:0.3rem; }
  .btn { display:inline-block; margin-top:0.9rem; background:var(--accent); color:#fff; padding:0.75rem 1.4rem; border-radius:9999px; font-weight:600; text-decoration:none; }
  .btn:hover { background:var(--accent-light); }
  .more { margin-top:2.5rem; padding-top:1.5rem; border-top:1px solid var(--border); }
  .more h3 { font-size:0.8rem; text-transform:uppercase; letter-spacing:0.06em; color:var(--dim); margin-bottom:0.6rem; }
  .foot { margin-top:2.5rem; font-size:0.78rem; color:var(--dim); }
  @media (max-width:600px) {
    .nav { padding:1rem; }
    .wrap { padding:2rem 1rem 3rem; }
    h1 { font-size:1.7rem; }
    .btn { display:block; text-align:center; }
  }
</style>
</head>
<body>
<nav class="nav">
  <a href="/"><img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="height:24px;width:auto;display:block;"></a>
  <a class="nav-cta" href="/tc-check?src=seo_""" + slug + """_nav">Check a file free</a>
</nav>
<main class="wrap">
  <div class="crumbs"><a href="/">Home</a> / <a href="/guides">Guides</a></div>
  <div class="kicker">""" + g["kicker"] + """</div>
  <h1>""" + g["h1"] + """</h1>
  <p class="lede">""" + g["lede"] + """</p>
  <div class="byline">By Phanel Jean Baptiste, builder of TxtAnOffer (a software builder, not a licensed agent) &middot; Last reviewed October 2026</div>
""" + body_html + cta + """
""" + ('<div class="more"><h3>More guides</h3><ul>' + others + '</ul></div>' if others else '') + """
  <p class="foot">General information only, not legal advice. TxtAnOffer is an independent tool and is not affiliated with or endorsed by the Texas Real Estate Commission (TREC). <a href="/terms">Terms</a> &middot; <a href="/privacy">Privacy</a></p>
</main>
</body>
</html>"""


@app.route("/guides")
def guides_index():
    items = "".join(
        f'<li><a href="/guides/{s}"><strong>{escape(g["h1"])}</strong></a><br><span style="color:#5a6b7a;font-size:0.9rem;">{escape(g["description"])}</span></li>'
        for s, g in _GUIDES.items()
    )
    body = '<h2>All guides</h2><ul style="list-style:none;margin-left:0;">' + items + "</ul>"
    page = _guide_page("index", {
        "title": "Guides for Texas TCs and Brokers",
        "description": "Plain-language guides to the TREC 20-19, the mistakes title kicks back, and Texas broker record-keeping rules.",
        "kicker": "Guides",
        "h1": "Guides for Texas transaction coordinators and brokers",
        "lede": "Plain-language guides to the TREC 20-19 and the mistakes that hold up Texas closings.",
    }, body)
    page = page.replace('<link rel="canonical" href="https://txtanoffer.com/guides/index">', '<link rel="canonical" href="https://txtanoffer.com/guides">')
    resp = make_response(page)
    track_page_view(resp, "guide_page")
    return resp


@app.route("/guides/<slug>")
def guide(slug):
    g = _GUIDES.get(slug)
    if not g:
        abort(404)
    resp = make_response(_guide_page(slug, g, g["body"]))
    track_page_view(resp, "guide_page")
    return resp


# --- Sign-up CRM (/admin/crm, 2026-10-07) -------------------------------------
# See crm.py. Auth: ?token=<ANALYTICS_PASSWORD> or the ta_internal cookie that
# /internal-mode (and this page's sign-in form) sets -- same secret as /analytics.

def _crm_authed():
    if not ANALYTICS_PASSWORD:
        return False
    tok = request.args.get("token", "") or request.cookies.get("ta_internal", "")
    return hmac.compare_digest(tok, ANALYTICS_PASSWORD)


_CRM_CSS = """
:root{--bg:#F5F5F7;--text:#0f1f2f;--muted:#5a6b7a;--dim:#8a9aa9;--green:#0b5d52;--gd:#0a3f3a;--tint:#E7F3F1;--y:#f5c242;--yt:#FFF6DA;--border:rgba(15,31,47,0.08);}
*{margin:0;padding:0;box-sizing:border-box;}
body{font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;background:var(--bg);color:var(--text);line-height:1.5;font-size:14px;-webkit-font-smoothing:antialiased;}
a{color:var(--green);}
.head{background:var(--gd);border-bottom:4px solid var(--y);padding:14px 24px;display:flex;justify-content:space-between;align-items:center;}
.head img{height:22px;display:block;}
.head span{color:var(--y);font-weight:800;font-size:0.72rem;letter-spacing:0.12em;text-transform:uppercase;}
.wrap{max-width:1180px;margin:0 auto;padding:1.5rem 1.25rem 4rem;}
h1{font-size:1.5rem;letter-spacing:-0.02em;}
h2{font-size:1.05rem;margin:0 0 0.6rem;}
.sub{color:var(--muted);margin:0.2rem 0 1.25rem;}
.card{background:#fff;border:1px solid var(--border);border-radius:14px;padding:1.1rem 1.2rem;margin-bottom:1.1rem;}
.funnel{display:grid;grid-template-columns:repeat(7,1fr);gap:8px;}
.fstep{background:#fff;border:1px solid var(--border);border-top:4px solid var(--y);border-radius:12px;padding:10px 12px;text-decoration:none;color:var(--text);}
.fstep b{display:block;font-size:1.4rem;color:var(--green);}
.fstep span{font-size:0.7rem;text-transform:uppercase;letter-spacing:0.05em;color:var(--dim);font-weight:700;}
.fstep.goal{background:var(--gd);border-top-color:var(--y);}
.fstep.goal b{color:var(--y);} .fstep.goal span{color:#c9dcd8;}
.kpi{margin-top:10px;font-size:0.9rem;color:var(--muted);}
.kpi b{color:var(--text);}
table{width:100%;border-collapse:collapse;}
th{text-align:left;font-size:0.68rem;text-transform:uppercase;letter-spacing:0.05em;color:var(--dim);padding:7px 8px;border-bottom:2px solid #eef0f2;}
td{padding:9px 8px;border-bottom:1px solid #eef0f2;vertical-align:top;}
.pill{display:inline-block;font-size:0.68rem;font-weight:700;padding:2px 8px;border-radius:999px;background:#eef0f2;color:var(--muted);white-space:nowrap;}
.pill.contacted{background:#e8eef7;color:#2c5282;} .pill.replied{background:var(--yt);color:#8a5a00;}
.pill.engaged{background:#fdf3e3;color:#b45309;} .pill.trial{background:var(--tint);color:var(--green);}
.pill.signed_up,.pill.paying{background:var(--gd);color:var(--y);} .pill.lost{background:#f3f4f6;color:#9ca3af;}
.why{font-size:0.78rem;color:var(--green);font-weight:600;}
.muted{color:var(--dim);font-size:0.78rem;}
input[type=text],input[type=email],input[type=date],select,textarea{width:100%;padding:7px 9px;border:1px solid #d5dbe0;border-radius:8px;font:inherit;font-size:0.85rem;background:#fff;}
textarea{min-height:70px;}
.btn{display:inline-block;background:var(--green);color:#fff;border:0;border-radius:999px;padding:6px 13px;font:inherit;font-weight:700;font-size:0.78rem;cursor:pointer;text-decoration:none;}
.btn.y{background:var(--y);color:var(--text);} .btn.ghost{background:#fff;color:var(--green);border:1.5px solid var(--green);}
.btn.red{background:#fff;color:#dc2626;border:1.5px solid #dc2626;}
.row{display:flex;gap:6px;flex-wrap:wrap;align-items:center;}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:1.1rem;}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;}
details summary{cursor:pointer;list-style:none;}
details summary::-webkit-details-marker{display:none;}
.flash{background:var(--tint);border-radius:10px;padding:0.7rem 1rem;margin-bottom:1rem;}
.today td:first-child{border-left:4px solid var(--y);}
@media(max-width:860px){.funnel{grid-template-columns:repeat(auto-fill,minmax(96px,1fr));}.fstep span{font-size:0.62rem;}.grid2,.grid4{grid-template-columns:1fr;}
 .tw table,.tw tbody,.tw tr,.tw td{display:block;width:100%;} .tw th{display:none;} .tw tr{border-bottom:1px solid #eef0f2;padding:8px 0;} .tw td{border:0;padding:3px 0;}}
"""


def _crm_page(body):
    return ("""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex"><title>Sign-up CRM — TxtAnOffer</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>""" + _CRM_CSS + """</style></head><body>
<div class="head"><a href="/"><img src="/static/logo-wordmark-white.png?v=1" alt="txtanoffer"></a><span>Sign-up CRM</span></div>
<main class="wrap">""" + body + "</main></body></html>")


@app.route("/admin/crm", methods=["GET", "POST"])
def admin_crm():
    if not _crm_authed():
        if request.method == "POST" and request.form.get("password"):
            if ANALYTICS_PASSWORD and hmac.compare_digest(request.form["password"], ANALYTICS_PASSWORD):
                resp = make_response(redirect("/admin/crm"))
                resp.set_cookie("ta_internal", ANALYTICS_PASSWORD, max_age=365 * 24 * 3600, httponly=True,
                                secure=request.is_secure or request.headers.get("X-Forwarded-Proto") == "https", samesite="Lax")
                return resp
        return _crm_page('<div class="card" style="max-width:420px;margin:3rem auto;"><h1>Sign in</h1><p class="sub">Same password as /analytics.</p>'
                         '<form method="post"><input type="password" name="password" required style="width:100%;padding:8px;border:1px solid #d5dbe0;border-radius:8px;">'
                         '<button class="btn" style="margin-top:10px;width:100%;">Open the CRM</button></form></div>'), 401

    flash = ""
    if request.method == "POST":
        a = request.form.get("action", "")
        lid = request.form.get("lead_id", type=int)
        if a == "add":
            crm.add_lead(**{k: request.form.get(k, "") for k in ("name", "firm", "email", "phone", "market", "role", "source", "src_tag", "stage", "next_action", "next_due", "notes")})
            flash = "Lead added."
        elif a == "import":
            r = crm.import_csv(request.form.get("csv", ""))
            flash = f"Imported: {r['added']} added, {r['merged']} merged with existing, {r['skipped']} skipped."
        elif a == "update" and lid:
            crm.update_lead(lid, **{k: request.form.get(k) for k in ("name", "firm", "email", "phone", "market", "src_tag", "stage", "next_action", "next_due", "notes")})
            flash = "Saved."
        elif a == "touch" and lid:
            crm.log_touch(lid, request.form.get("kind", "note"), request.form.get("note", ""))
            if request.form.get("next_due") or request.form.get("next_action"):
                crm.update_lead(lid, next_due=request.form.get("next_due") or None, next_action=request.form.get("next_action") or None)
            flash = "Logged."
        elif a == "delete" and lid:
            crm.delete_lead(lid)
            flash = "Lead deleted."
        elif a == "add_inbound":
            crm.add_lead(email=request.form.get("email", ""), source="inbound", stage="trial",
                         next_action="Reach out: offer to walk through their report", next_due=datetime.utcnow().date().isoformat())
            flash = "Added to the CRM."
        elif a == "dismiss_inbound":
            crm.dismiss_inbound(request.form.get("email", ""))
            flash = "Dismissed."
        return redirect("/admin/crm?flash=" + _urlquote(flash) + ("&q=" + _urlquote(request.form.get("q", "")) if request.form.get("q") else ""))

    flash = request.args.get("flash", "")
    q = (request.args.get("q") or "").strip()[:100]
    stage_f = request.args.get("stage", "")
    every = crm.list_leads()
    leads = crm.list_leads(q, stage_f) if (q or stage_f) else every

    counts = {s: 0 for s in crm.STAGES}
    for l in every:
        counts[l["stage"]] += 1
    reached = sum(counts[s] for s in crm.STAGES if s not in ("new", "lost"))
    won = counts["signed_up"] + counts["paying"]
    rate = f"{(won / reached * 100):.0f}%" if reached else "—"
    funnel = "".join(
        f'<a class="fstep{" goal" if s == "signed_up" else ""}" href="/admin/crm?stage={s}"><b>{counts[s]}</b><span>{crm.STAGE_LABELS[s]}</span></a>'
        for s in crm.STAGES if s != "lost")

    today = sorted([l for l in every if l["score"] > 0], key=lambda l: (-l["score"], l["next_due"] or "9999"))[:12]

    def quick_forms(l):
        return "".join(
            f'<form method="post" style="display:inline;"><input type="hidden" name="action" value="touch"><input type="hidden" name="lead_id" value="{l["id"]}">'
            f'<input type="hidden" name="kind" value="{k}"><button class="btn {cls}">{lbl}</button></form> '
            for k, lbl, cls in (("call", "Called", "ghost"), ("email", "Emailed", "ghost"), ("reply", "They replied", "y")))

    today_rows = "".join(
        f'<tr class="today"><td><b>{escape(l["name"] or l["email"])}</b><div class="muted">{escape(l["firm"])}{" · " + escape(l["market"]) if l["market"] else ""}</div></td>'
        f'<td><span class="pill {l["stage"]}">{crm.STAGE_LABELS[l["stage"]]}</span><div class="why">{escape(", ".join(l["reasons"]))}</div></td>'
        f'<td>{escape(l["next_action"] or "Ask for a 10-minute walkthrough of their report")}<div class="muted">{("due " + escape(l["next_due"])) if l["next_due"] else ""}</div></td>'
        f'<td>{("<a href=" + chr(39) + "tel:" + escape(l["phone"]) + chr(39) + ">" + escape(l["phone"]) + "</a><br>") if l["phone"] else ""}{("<a href=" + chr(39) + "mailto:" + escape(l["email"]) + chr(39) + ">" + escape(l["email"]) + "</a>") if l["email"] else ""}</td>'
        f'<td class="row">{quick_forms(l)}</td></tr>'
        for l in today) or '<tr><td colspan="5" class="muted" style="padding:14px 8px;">Nobody needs action right now. Add leads below, or log calls and replies as they happen.</td></tr>'

    inbound = crm.inbound_not_in_crm()[:20]
    inbound_rows = "".join(
        f'<tr><td><b>{escape(i["email"])}</b><div class="why">{escape(i["why"])}</div></td><td class="muted">{escape(i["last"][:10])}</td>'
        f'<td class="row"><form method="post" style="display:inline;"><input type="hidden" name="action" value="add_inbound"><input type="hidden" name="email" value="{escape(i["email"])}"><button class="btn y">Add to CRM</button></form> '
        f'<form method="post" style="display:inline;"><input type="hidden" name="action" value="dismiss_inbound"><input type="hidden" name="email" value="{escape(i["email"])}"><button class="btn ghost">Dismiss</button></form></td></tr>'
        for i in inbound) or '<tr><td colspan="3" class="muted">No new intent signals.</td></tr>'

    def stage_opts(cur):
        return "".join(f'<option value="{s}"{" selected" if s == cur else ""}>{crm.STAGE_LABELS[s]}</option>' for s in crm.STAGES)

    def lead_row(l):
        hist = "".join(f'<div class="muted">{escape(t["created_at"][:10])} · <b>{escape(t["kind"])}</b> {escape(t["note"])}</div>' for t in l["touches"][:8]) or '<div class="muted">No touches logged yet.</div>'
        return f"""<tr><td><details><summary><b>{escape(l['name'] or l['email'] or l['phone'])}</b><div class="muted">{escape(l['firm'])}{' · ' + escape(l['market']) if l['market'] else ''}</div></summary>
<div style="margin-top:10px;" class="grid2">
 <form method="post"><input type="hidden" name="action" value="update"><input type="hidden" name="lead_id" value="{l['id']}">
  <div class="grid4"><input type="text" name="name" value="{escape(l['name'])}" placeholder="Name"><input type="text" name="firm" value="{escape(l['firm'])}" placeholder="Firm">
  <input type="email" name="email" value="{escape(l['email'])}" placeholder="Email"><input type="text" name="phone" value="{escape(l['phone'])}" placeholder="Phone">
  <input type="text" name="market" value="{escape(l['market'])}" placeholder="Market"><input type="text" name="src_tag" value="{escape(l['src_tag'])}" placeholder="Link tag (src)">
  <select name="stage">{stage_opts(l['stage'] if l['stage'] in crm.STAGES else 'new')}</select><input type="date" name="next_due" value="{escape(l['next_due'])}"></div>
  <input type="text" name="next_action" value="{escape(l['next_action'])}" placeholder="Next action" style="margin-top:8px;">
  <textarea name="notes" placeholder="Notes" style="margin-top:8px;">{escape(l['notes'])}</textarea>
  <div class="row" style="margin-top:8px;"><button class="btn">Save</button></div></form>
 <div><form method="post"><input type="hidden" name="action" value="touch"><input type="hidden" name="lead_id" value="{l['id']}">
  <div class="row"><select name="kind" style="width:auto;">{''.join(f'<option>{k}</option>' for k in crm.TOUCH_KINDS)}</select><input type="text" name="note" placeholder="What happened?" style="flex:1;width:auto;"></div>
  <div class="row" style="margin-top:6px;"><input type="text" name="next_action" placeholder="Next action (optional)" style="flex:1;width:auto;"><input type="date" name="next_due" style="width:auto;"><button class="btn y">Log it</button></div></form>
  <div style="margin-top:10px;">{hist}</div>
  <form method="post" style="margin-top:10px;" onsubmit="return confirm('Delete this lead?');"><input type="hidden" name="action" value="delete"><input type="hidden" name="lead_id" value="{l['id']}"><button class="btn red">Delete lead</button></form></div>
</div></details></td>
<td><span class="pill {l['stage']}">{crm.STAGE_LABELS[l['stage']]}</span>{'<div class="why">' + escape(', '.join(l['reasons'])) + '</div>' if l['reasons'] else ''}</td>
<td>{escape(l['next_action'])}<div class="muted">{escape(l['next_due'])}</div></td>
<td class="muted">{escape(l['source'])}{'<br>src=' + escape(l['src_tag']) if l['src_tag'] else ''}</td></tr>"""

    all_rows = "".join(lead_row(l) for l in leads) or '<tr><td colspan="4" class="muted">No leads match.</td></tr>'
    filt = f'Showing {len(leads)} of {len(every)}' + (f' · <a href="/admin/crm">clear filter</a>' if (q or stage_f) else '')

    body = f"""
<h1>Sign-up CRM</h1>
<p class="sub">Every lead, ranked by how close they are to signing up. Product activity (checked a file, clicked their link, joined the archive list, signed up) moves them forward automatically.</p>
{('<div class="flash">' + escape(flash) + '</div>') if flash else ''}
<div class="card"><div class="funnel">{funnel}</div>
<div class="kpi">Reached: <b>{reached}</b> · Signed up or paying: <b>{won}</b> · Sign-up rate from reached: <b>{rate}</b> · Not a fit: {counts['lost']}</div></div>

<div class="card"><h2>Do these today</h2><p class="muted" style="margin:-0.3rem 0 0.6rem;">Highest intent first. Strongest signals: tried the product, replied, clicked their link, follow-up due.</p>
<div class="tw"><table><tr><th>Lead</th><th>Why now</th><th>Next action</th><th>Contact</th><th>Log</th></tr>{today_rows}</table></div></div>

<div class="card"><h2>New intent, not in the CRM yet</h2><p class="muted" style="margin:-0.3rem 0 0.6rem;">People who used TC Check with an email or joined the archive list. Add them, or dismiss tests and spam.</p>
<div class="tw"><table><tr><th>Email</th><th>Last</th><th></th></tr>{inbound_rows}</table></div></div>

<div class="card"><div class="row" style="justify-content:space-between;margin-bottom:0.6rem;"><h2 style="margin:0;">All leads</h2>
<form method="get" class="row"><input type="text" name="q" value="{escape(q)}" placeholder="Search name, firm, email, market" style="width:260px;"><button class="btn">Search</button></form></div>
<p class="muted" style="margin-bottom:0.5rem;">{filt} · Click a name to edit it or log a touch.</p>
<div class="tw"><table><tr><th>Lead</th><th>Stage</th><th>Next action</th><th>Source</th></tr>{all_rows}</table></div></div>

<div class="grid2">
<div class="card"><h2>Add a lead</h2><form method="post"><input type="hidden" name="action" value="add">
<div class="grid4"><input type="text" name="name" placeholder="Name"><input type="text" name="firm" placeholder="Firm"><input type="email" name="email" placeholder="Email"><input type="text" name="phone" placeholder="Phone">
<input type="text" name="market" placeholder="Market"><input type="text" name="source" placeholder="Source (e.g. zillow)"><input type="text" name="src_tag" placeholder="Link tag (src)"><select name="stage">{stage_opts('new')}</select></div>
<input type="text" name="next_action" placeholder="Next action" style="margin-top:8px;"><button class="btn" style="margin-top:8px;">Add lead</button></form></div>
<div class="card"><h2>Import (CSV)</h2><form method="post"><input type="hidden" name="action" value="import">
<textarea name="csv" placeholder="name,firm,email,phone,market,source,src_tag,stage,next_action,next_due"></textarea>
<p class="muted" style="margin:6px 0;">Header row required. Same email = merged, not duplicated.</p><button class="btn">Import</button></form></div>
</div>
"""
    return _crm_page(body)


# --- Brokerage archive: sign-in + archive pages (2026-10-07) -----------------
# See archive.py (storage) and broker_auth.py (sign-in links + session).

_BROKER_PAGE_CSS = """
  :root{--bg:#F5F5F7;--text:#0f1f2f;--muted:#5a6b7a;--dim:#8a9aa9;--green:#0b5d52;--green-dark:#0a3f3a;--tint:#E7F3F1;--yellow:#f5c242;--border:rgba(15,31,47,0.08);}
  *{margin:0;padding:0;box-sizing:border-box;}
  body{font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;background:var(--bg);color:var(--text);line-height:1.55;-webkit-font-smoothing:antialiased;}
  a{color:var(--green);}
  .head{background:var(--green-dark);padding:16px 24px;display:flex;justify-content:space-between;align-items:center;gap:12px;}
  .head img{height:22px;display:block;}
  .head .who{color:#c9dcd8;font-size:0.8rem;}
  .head .who a{color:var(--yellow);font-weight:600;margin-left:10px;}
  .bar{height:4px;background:var(--yellow);}
  .wrap{max-width:980px;margin:0 auto;padding:2rem 1.25rem 3rem;}
  h1{font-size:1.7rem;letter-spacing:-0.02em;margin-bottom:0.3rem;}
  .sub{color:var(--muted);font-size:0.92rem;margin-bottom:1.5rem;}
  .card{background:#fff;border:1px solid var(--border);border-radius:14px;padding:1.25rem;margin-bottom:1.25rem;}
  .card h2{font-size:1.05rem;margin-bottom:0.4rem;}
  .card p{font-size:0.88rem;color:var(--muted);}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:1.25rem;}
  input[type=text],input[type=email]{width:100%;padding:0.65rem 0.8rem;border:1px solid #d5dbe0;border-radius:10px;font:inherit;font-size:0.92rem;}
  .btn{display:inline-block;background:var(--green);color:#fff;border:0;border-radius:999px;padding:0.65rem 1.2rem;font:inherit;font-weight:700;font-size:0.88rem;cursor:pointer;text-decoration:none;}
  .btn.ghost{background:#fff;color:var(--green);border:1.5px solid var(--green);}
  .btn.small{padding:0.35rem 0.8rem;font-size:0.78rem;}
  .drop{border:2px dashed #cfd6dc;border-radius:12px;padding:1.1rem;text-align:center;margin:0.75rem 0;background:#fafbfb;}
  .drop input{margin-top:0.5rem;}
  code{background:var(--tint);padding:2px 6px;border-radius:6px;font-size:0.85rem;}
  table{width:100%;border-collapse:collapse;font-size:0.86rem;}
  th{text-align:left;font-size:0.7rem;text-transform:uppercase;letter-spacing:0.05em;color:var(--dim);padding:8px;border-bottom:2px solid #eef0f2;}
  td{padding:9px 8px;border-bottom:1px solid #eef0f2;vertical-align:top;}
  .tag{display:inline-block;font-size:0.68rem;font-weight:700;padding:2px 7px;border-radius:5px;}
  .tag.ok{background:#ecfdf5;color:#047857;}
  .tag.bad{background:#fdecec;color:#dc2626;}
  .tag.warn{background:#fdf3e3;color:#b45309;}
  .tag.na{background:#eef0f2;color:var(--muted);}
  .flash{background:var(--tint);border-radius:10px;padding:0.8rem 1rem;margin-bottom:1rem;font-size:0.9rem;}
  .flash.err{background:#fdecec;}
  .muted{color:var(--dim);font-size:0.78rem;}
  .tablewrap{overflow-x:auto;}
  @media(max-width:720px){.grid{grid-template-columns:1fr;}h1{font-size:1.4rem;}
    .tablewrap table,.tablewrap tbody,.tablewrap tr,.tablewrap td{display:block;width:100%;}
    .tablewrap th{display:none;}
    .tablewrap tr{padding:10px 0;border-bottom:1px solid #eef0f2;}
    .tablewrap td{border:0;padding:3px 0;}}
"""


def _broker_shell(title, body, who=""):
    return ("""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><meta name="robots" content="noindex">
<title>""" + escape(title) + """ — TxtAnOffer</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>""" + _BROKER_PAGE_CSS + """</style></head><body>
<div class="head"><a href="/"><img src="/static/logo-wordmark-white.png?v=1" alt="txtanoffer"></a><span class="who">""" + who + """</span></div>
<div class="bar"></div><main class="wrap">""" + body + """</main></body></html>""")


def _current_broker():
    """(brokerage, email, cookie) for a valid session whose email is still
    one of the brokerage's login emails, else None."""
    raw = request.cookies.get(broker_auth.COOKIE, "")
    sess = broker_auth.read_session(raw)
    if not sess:
        return None
    bid, email = sess
    b = get_brokerage(bid)
    if not b or email not in archive_store.login_emails_for(b):
        return None
    return b, email, raw


@app.route("/broker/login", methods=["GET", "POST"])
def broker_login():
    msg = ""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()[:120]
        ip = request.remote_addr or "unknown"
        if check_and_increment(f"broker_login:{ip}", limit=8) and check_and_increment(f"broker_login_e:{email.lower()}", limit=5):
            b = archive_store.find_brokerage_for_login(email)
            if b:
                p = broker_auth.login_link_params(b["id"], email)
                link = request.host_url.rstrip("/") + "/broker/login/verify?" + "&".join(f"{k}={_quote(str(v))}" for k, v in p.items())
                send_html_email(
                    email, "Your TxtAnOffer sign-in link",
                    f"Sign in to {b['name']}'s archive on TxtAnOffer:\n{link}\n\nThis link works once and expires in 20 minutes. If you didn't ask for it, ignore this email.",
                    f'<div style="font-family:Arial,sans-serif;font-size:14px;color:#0f1f2f;"><p>Sign in to <strong>{escape(b["name"])}</strong>&rsquo;s archive on TxtAnOffer:</p>'
                    f'<p><a href="{escape(link)}" style="display:inline-block;background:#0b5d52;color:#fff;padding:10px 18px;border-radius:999px;text-decoration:none;font-weight:700;">Sign in &rarr;</a></p>'
                    f'<p style="color:#5a6b7a;font-size:12px;">This link expires in 20 minutes. If you didn&rsquo;t ask for it, ignore this email.</p></div>',
                )
                track_event("broker_login_link_sent", None, {"brokerage_id": b["id"]})
        # Same answer either way, so this page can't be used to test which emails are customers.
        msg = '<div class="flash">If that email belongs to a TxtAnOffer brokerage, a sign-in link is on its way. Check your inbox (and spam).</div>'
    body = ("""<div class="card" style="max-width:460px;margin:2rem auto;">
  <h1>Sign in to your archive</h1>
  <p class="sub">We&rsquo;ll email you a one-time sign-in link. No password needed.</p>
  """ + msg + """
  <form method="post"><input type="email" name="email" required placeholder="you@brokerage.com" autocomplete="email">
  <button class="btn" style="margin-top:0.8rem;width:100%;">Email me a sign-in link</button></form>
  <p class="muted" style="margin-top:1rem;">Use the email your brokerage signed up with. Not a customer yet? <a href="/brokers">Start with a free 20-file audit</a>.</p>
</div>""")
    return _broker_shell("Sign in", body)


def _quote(v):
    from urllib.parse import quote
    return quote(v, safe="")


@app.route("/broker/login/verify")
def broker_login_verify():
    bid = broker_auth.verify_login_link(request.args.get("b"), request.args.get("e"), request.args.get("exp"), request.args.get("sig"))
    b = get_brokerage(bid) if bid else None
    email = (request.args.get("e") or "").lower()
    if not b or email not in archive_store.login_emails_for(b):
        return _broker_shell("Link expired", '<div class="card" style="max-width:460px;margin:2rem auto;"><h1>That link has expired</h1><p class="sub">Sign-in links work for 20 minutes.</p><a class="btn" href="/broker/login">Get a new link</a></div>'), 400
    resp = make_response(redirect("/broker/archive"))
    resp.set_cookie(broker_auth.COOKIE, broker_auth.session_value(b["id"], email), max_age=broker_auth.SESSION_TTL,
                    httponly=True, secure=request.is_secure or request.headers.get("X-Forwarded-Proto") == "https", samesite="Lax")
    track_event("broker_login", None, {"brokerage_id": b["id"]})
    return resp


@app.route("/broker/logout")
def broker_logout():
    resp = make_response(redirect("/broker/login"))
    resp.delete_cookie(broker_auth.COOKIE)
    return resp


def _archive_status_tag(f):
    from tc_check_email import _display_issues
    if not f.get("recognized"):
        return '<span class="tag na">Stored, not checked</span>'
    shown = _display_issues(f.get("issues") or [])  # same grouping as the report
    nb = sum(1 for i in shown if i.get("severity") == "blocker")
    if nb:
        return f'<span class="tag bad">{nb} must fix</span>'
    if shown:
        return f'<span class="tag warn">{len(shown)} to review</span>'
    return '<span class="tag ok">Clear</span>'


@app.route("/broker/archive", methods=["GET", "POST"])
def broker_archive():
    cur = _current_broker()
    if not cur:
        return redirect("/broker/login")
    b, email, raw = cur
    csrf = broker_auth.csrf_token(raw)
    flash = ""

    if request.method == "POST":
        if not hmac.compare_digest(request.form.get("csrf", ""), csrf):
            abort(400)
        action = request.form.get("action")
        if action == "toggle_forwards":
            archive_store.set_archive_forwards(b["id"], request.form.get("on") == "1")
            return redirect("/broker/archive")
        if action == "upload":
            files = [f for f in request.files.getlist("files") if f and f.filename][:20]
            saved = dupes = skipped = 0
            for f in files:
                if not f.filename.lower().endswith(".pdf"):
                    skipped += 1
                    continue
                data = f.read()
                if len(data) > archive_store.MAX_FILE_BYTES:
                    skipped += 1
                    continue
                result = None
                tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
                try:
                    tmp.write(data)
                    tmp.close()
                    try:
                        result = check_tc_file([tmp.name])
                    except Exception:
                        result = None  # not a readable TREC 20-19 -- still archived, marked "not checked"
                finally:
                    try:
                        os.remove(tmp.name)
                    except OSError:
                        pass
                rec = archive_store.save_file(b["id"], data, f.filename, "upload", email, result)
                dupes += 1 if rec.get("duplicate") else 0
                saved += 0 if rec.get("duplicate") else 1
            track_event("archive_saved", None, {"brokerage_id": b["id"], "source": "upload", "files": saved, "duplicates": dupes})
            parts = [f"{saved} file{'s' if saved != 1 else ''} saved"]
            if dupes:
                parts.append(f"{dupes} already in your archive")
            if skipped:
                parts.append(f"{skipped} skipped (PDFs up to 25 MB only)")
            flash = '<div class="flash">' + escape(" · ".join(parts)) + "</div>"

    q = (request.args.get("q") or "").strip()[:100]
    files = archive_store.list_files(b["id"], q)
    rows = "".join(
        f"<tr><td style='white-space:nowrap;'>{escape(f['created_at'][:10])}</td>"
        f"<td><strong>{escape(f['address'] or '—')}</strong><div class='muted'>{escape(', '.join(x for x in (f['city'], (f['county'] + ' County') if f['county'] else '') if x))}</div></td>"
        f"<td>{escape(f['original_name'] or '')}<div class='muted'>{'Forwarded by ' + escape(f['sender']) if f['source'] == 'forward' else 'Uploaded by ' + escape(f['sender'])}</div></td>"
        f"<td>{_archive_status_tag(f)}</td>"
        f"<td style='white-space:nowrap;'><a class='btn small ghost' href='/broker/archive/file/{f['id']}'>Download</a> "
        f"<form method='post' action='/broker/archive/file/{f['id']}/delete' style='display:inline;' onsubmit=\"return confirm('Delete this file from your archive? This can\\'t be undone.');\">"
        f"<input type='hidden' name='csrf' value='{csrf}'><button class='btn small ghost' style='border-color:#dc2626;color:#dc2626;'>Delete</button></form></td></tr>"
        for f in files
    ) or f"<tr><td colspan='5' class='muted' style='padding:14px 8px;'>{'No files match &ldquo;' + escape(q) + '&rdquo;.' if q else 'No files yet. Forward a contract or drop one above.'}</td></tr>"

    drafts = list_brokerage_records(b["id"], q)
    draft_rows = "".join(
        f"<tr><td>{escape((r['created_at'] or '')[:10])}</td><td>{escape(r['address'] or '')}</td>"
        f"<td>{'Offer (20-19)' if r['kind'] == 'offer' else 'Amendment (39-11)'}</td><td>{escape(r['phone'] or '')}</td></tr>"
        for r in drafts[:200]
    ) or "<tr><td colspan='4' class='muted'>No drafts yet.</td></tr>"

    fwd_on = bool(b.get("archive_forwards", 1))
    who = escape(email) + ' <a href="/broker/logout">Sign out</a>'
    body = f"""
<h1>{escape(b['name'])} &mdash; contract archive</h1>
<p class="sub">Executed contracts kept 5 years, checked on the way in, searchable by address. Only people who sign in with your brokerage&rsquo;s email can see them.</p>
{flash}
<div class="grid">
  <div class="card">
    <h2>Forward it</h2>
    <p>Forward any executed contract to <code>tc@check.txtanoffer.com</code> from <strong>{escape(b.get('tc_email') or 'your brokerage email')}</strong> or from an agent on your roster. You&rsquo;ll get the check report back, and the PDF lands here.</p>
    <form method="post" style="margin-top:0.75rem;"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="action" value="toggle_forwards"><input type="hidden" name="on" value="{'0' if fwd_on else '1'}">
      <span class="tag {'ok' if fwd_on else 'na'}">Saving forwards: {'On' if fwd_on else 'Off'}</span>
      <button class="btn small ghost" style="margin-left:6px;">{'Turn off' if fwd_on else 'Turn on'}</button></form>
  </div>
  <div class="card">
    <h2>Or drop it</h2>
    <form method="post" enctype="multipart/form-data"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="action" value="upload">
      <div class="drop">PDFs only &middot; up to 20 at a time, 25 MB each<br><input type="file" name="files" accept="application/pdf,.pdf" multiple required></div>
      <button class="btn">Save to archive</button></form>
  </div>
</div>
<div class="card">
  <form method="get" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:0.8rem;">
    <input type="text" name="q" value="{escape(q)}" placeholder="Search by address, e.g. 123 Main St" style="flex:1;min-width:200px;">
    <button class="btn">Search</button>
    <a class="btn ghost" href="/broker/archive/export.zip">Download all (ZIP)</a>
  </form>
  <div class="tablewrap"><table><tr><th>Added</th><th>Property</th><th>File</th><th>Check</th><th></th></tr>{rows}</table></div>
</div>
<div class="card">
  <h2>Drafts created in TxtAnOffer</h2>
  <p style="margin-bottom:0.6rem;">Offers and amendments your agents texted in, kept 5 years. These are drafts, not the executed contracts.</p>
  <div class="tablewrap"><table><tr><th>Date</th><th>Address</th><th>Document</th><th>Agent</th></tr>{draft_rows}</table></div>
</div>
<p class="muted">Files are kept 5 years from when they&rsquo;re added &mdash; longer than the 4 years TREC requires brokers to keep transaction records (22 TAC &sect;535.2). Deleting a file removes it permanently. Keep your own copies as well. TxtAnOffer is not affiliated with TREC.</p>
"""
    return _broker_shell(f"{b['name']} archive", body, who)


@app.route("/broker/archive/file/<int:file_id>")
def broker_archive_file(file_id):
    cur = _current_broker()
    if not cur:
        return redirect("/broker/login")
    f = archive_store.get_file(file_id, cur[0]["id"])
    if not f or not os.path.isfile(f["path"]):
        abort(404)
    track_event("archive_download", None, {"brokerage_id": cur[0]["id"], "file_id": file_id})
    return send_from_directory(os.path.dirname(f["path"]), os.path.basename(f["path"]), as_attachment=True,
                               download_name=re.sub(r"[^A-Za-z0-9._ -]", "_", f["original_name"] or "contract.pdf"))


@app.route("/broker/archive/file/<int:file_id>/delete", methods=["POST"])
def broker_archive_delete(file_id):
    cur = _current_broker()
    if not cur:
        return redirect("/broker/login")
    if not hmac.compare_digest(request.form.get("csrf", ""), broker_auth.csrf_token(cur[2])):
        abort(400)
    archive_store.delete_file(file_id, cur[0]["id"])
    track_event("archive_delete", None, {"brokerage_id": cur[0]["id"], "file_id": file_id})
    return redirect("/broker/archive")


@app.route("/broker/archive/export.zip")
def broker_archive_export():
    """Everything this brokerage has: archived executed files + drafts."""
    import io
    import zipfile
    cur = _current_broker()
    if not cur:
        return redirect("/broker/login")
    b = cur[0]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in archive_store.list_files(b["id"], limit=100000):
            path = os.path.join(archive_store.ARCHIVE_DIR, str(b["id"]), f["stored_name"])
            if os.path.isfile(path):
                safe = re.sub(r"[^A-Za-z0-9._ -]", "_", f["original_name"] or "contract.pdf")
                zf.write(path, arcname=f"executed/{f['created_at'][:10]}_{f['id']}_{safe}")
        for r in list_brokerage_records(b["id"], limit=100000):
            path = os.path.join(OUTPUT_DIR, r["filename"])
            if "/" not in r["filename"] and os.path.isfile(path):
                zf.write(path, arcname=f"drafts/{(r['created_at'] or '')[:10]}_{r['filename']}")
    track_event("brokerage_archive_export", None, {"brokerage_id": b["id"], "scope": "full"})
    safe_name = re.sub(r"[^A-Za-z0-9]+", "-", b["name"]).strip("-") or "brokerage"
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{safe_name}-archive.zip"'})


_ARCHIVE_EA_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Contract Archive — TxtAnOffer</title>
<meta name="description" content="On the TxtAnOffer Brokerage plan: forward or drop executed contracts, get each one checked for what title kicks back, and keep them 5 years, searchable by address.">
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  :root{--bg:#F5F5F7;--text:#0f1f2f;--muted:#5a6b7a;--dim:#8a9aa9;--green:#0b5d52;--green-dark:#0a3f3a;--tint:#E7F3F1;--yellow:#f5c242;--border:rgba(15,31,47,0.08);}
  *{margin:0;padding:0;box-sizing:border-box;}
  body{font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;background:var(--bg);color:var(--text);line-height:1.6;-webkit-font-smoothing:antialiased;}
  .head{background:var(--green-dark);padding:18px 24px;display:flex;justify-content:space-between;align-items:center;}
  .head img{height:22px;display:block;}
  .pill{background:var(--yellow);color:var(--text);font-size:0.68rem;font-weight:800;letter-spacing:0.1em;text-transform:uppercase;padding:5px 11px;border-radius:999px;}
  .bar{height:4px;background:var(--yellow);}
  .wrap{max-width:640px;margin:0 auto;padding:2.5rem 1.25rem 3rem;}
  h1{font-size:2rem;line-height:1.15;letter-spacing:-0.03em;font-weight:800;margin-bottom:0.8rem;text-wrap:balance;}
  .lede{color:var(--muted);font-size:1.02rem;margin-bottom:1.5rem;}
  .ways{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:1.25rem;}
  .way{background:#fff;border:1px solid var(--border);border-radius:14px;padding:16px;}
  .way b{display:block;font-size:0.98rem;margin-bottom:2px;}
  .way span{font-size:0.85rem;color:var(--muted);}
  ul{list-style:none;margin:0 0 1.75rem;}
  li{padding:6px 0 6px 26px;position:relative;font-size:0.95rem;}
  li::before{content:"\\2713";position:absolute;left:0;color:var(--green);font-weight:800;}
  form{background:#fff;border:1px solid var(--border);border-radius:16px;padding:1.4rem;border-top:4px solid var(--yellow);}
  form h2{font-size:1.15rem;margin-bottom:0.3rem;}
  form p{font-size:0.85rem;color:var(--muted);margin-bottom:1rem;}
  label{display:block;font-size:0.8rem;font-weight:600;margin:0.7rem 0 0.3rem;}
  input,select{width:100%;padding:0.7rem 0.8rem;border:1px solid #d5dbe0;border-radius:10px;font:inherit;font-size:0.95rem;background:#fff;}
  button{margin-top:1.1rem;width:100%;background:var(--green);color:#fff;border:0;border-radius:999px;padding:0.85rem;font:inherit;font-weight:700;font-size:0.95rem;cursor:pointer;}
  button:hover{background:#16806e;}
  .note{font-size:0.75rem;color:var(--dim);margin-top:1.25rem;}
  .ok{background:var(--tint);border-radius:14px;padding:1.25rem;font-size:0.95rem;}
  @media(max-width:520px){.ways{grid-template-columns:1fr;}h1{font-size:1.65rem;}}
</style>
</head>
<body>
<div class="head"><a href="/"><img src="/static/logo-wordmark-white.png?v=1" alt="txtanoffer"></a><span class="pill">Brokerage plan</span></div>
<div class="bar"></div>
<main class="wrap">
  <h1>Every executed contract. Checked. Kept for 5 years.</h1>
  <p class="lede">On the Brokerage plan: send in your executed TREC contracts, get each one checked for what title kicks back, and find any file by address years later. <a href="/broker/login" style="color:var(--green);font-weight:600;">Already a customer? Sign in &rarr;</a></p>
  <div class="ways">
    <div class="way"><b>Forward it</b><span>Send the executed contract to tc@check.txtanoffer.com from your brokerage email.</span></div>
    <div class="way"><b>Or drop it</b><span>Upload it in your archive, up to 20 files at a time.</span></div>
  </div>
  <ul>
    <li>Checked on the way in: blank fields, missing initials, 40-11 mismatches</li>
    <li>Searchable by property address</li>
    <li>Kept 5 years &mdash; longer than TREC&rsquo;s 4-year record rule (22 TAC &sect;535.2)</li>
    <li>Private to your brokerage, export everything anytime</li>
  </ul>
  __FORM__
  <p class="note">Included in the Brokerage plan. Files checked outside your archive are still deleted after the check (bulk uploads: when the batch finishes). TxtAnOffer is not affiliated with TREC. <a href="/" style="color:var(--green);">txtanoffer.com</a></p>
</main>
</body>
</html>"""

_ARCHIVE_EA_FORM = """<form method="post" action="/archive">
    <h2>Want it for your brokerage?</h2>
    <p>Leave your email and we&rsquo;ll set you up &mdash; starting with a free audit of your last 20 closed files.</p>
    <label for="email">Work email</label>
    <input type="email" id="email" name="email" required maxlength="120" placeholder="you@brokerage.com">
    <label for="brokerage">Brokerage name (optional)</label>
    <input type="text" id="brokerage" name="brokerage" maxlength="120">
    <label for="agents">Agents on your roster (optional)</label>
    <select id="agents" name="agents"><option value="">Choose one</option><option>1&ndash;10</option><option>11&ndash;50</option><option>51&ndash;150</option><option>150+</option></select>
    <button type="submit">Get set up</button>
  </form>"""


@app.route("/archive", methods=["GET", "POST"])
def archive_early_access():
    """Early-access sign-up for the drop/forward contract archive, which is
    NOT built yet (2026-10-07) -- the page says so plainly. Sign-ups are
    demand validation before building it; listed on /analytics."""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()[:120]
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return make_response(_ARCHIVE_EA_PAGE.replace("__FORM__", '<div class="ok" style="background:#fdecec;">That email doesn&rsquo;t look right. <a href="/archive">Try again</a>.</div>'), 400)
        if check_and_increment(f"archive_ea:{request.remote_addr or 'unknown'}", limit=5):
            track_event("archive_early_access", None, {
                "email": email,
                "brokerage": (request.form.get("brokerage") or "").strip()[:120],
                "agents": (request.form.get("agents") or "").strip()[:20],
                "source": request.cookies.get("ta_src") or "direct",
            })
        return _ARCHIVE_EA_PAGE.replace("__FORM__", '<div class="ok"><b>Thanks &mdash; we&rsquo;ll be in touch shortly</b> to set up your brokerage. In the meantime you can <a href="/tc-check" style="color:#0b5d52;font-weight:600;">check a file free</a>.</div>')
    src = re.sub(r"[^a-zA-Z0-9_-]", "", request.args.get("src", ""))[:60]
    resp = make_response(_ARCHIVE_EA_PAGE.replace("__FORM__", _ARCHIVE_EA_FORM))
    if src and not request.cookies.get("ta_src"):
        resp.set_cookie("ta_src", src, max_age=30 * 24 * 3600, httponly=True, samesite="Lax")
        track_event("landing_visit", None, {"source": src})
    track_page_view(resp, "archive_page")
    return resp


@app.route("/brokers")
def brokers():
    """Managing-broker landing page. The offer is the free 20-file backlog
    audit (the existing /tc-check/bulk sample tier), so a broker sees their
    own agents' files before being asked to pay for the Brokerage plan.
    Added 2026-10-06 after the vertical-SaaS site audit: every brokerage
    URL 404'd and the only broker pitch was one card on /pricing."""
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>For Managing Brokers — TxtAnOffer</title>
<meta name="description" content="Run your agents' last 20 closed TREC 20-19 files through TC Check, free. See which fields they leave blank before title calls you about it.">
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
    --accent: #0b5d52;
    --accent-light: #16806e;
    --accent-dark: #0a3a33;
    --accent-tint: #E7F3F1;
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
  .container {max-width:720px;margin:0 auto;padding:3.5rem 2rem 4rem;}
  .kicker {font-size:0.8rem;color:var(--accent-dark);font-weight:700;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:0.75rem;}
  h1 {font-size:2.3rem;font-weight:800;letter-spacing:-0.03em;line-height:1.15;margin-bottom:1rem;color:var(--text);}
  .lede {font-size:1.05rem;color:var(--text-muted);margin-bottom:2rem;}
  .lede strong {color:var(--text);font-weight:600;}
  h2 {font-size:1.3rem;font-weight:700;letter-spacing:-0.02em;margin:2.75rem 0 1rem;color:var(--text);}
  p {font-size:0.95rem;color:var(--text-muted);margin-bottom:1rem;}
  .offer {
    background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
    padding:1.75rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);border-top:4px solid var(--accent);
  }
  .offer h2 {margin-top:0;}
  .offer ol {margin:0 0 1.25rem 1.2rem;color:var(--text-muted);font-size:0.95rem;}
  .offer li {margin-bottom:0.5rem;}
  .offer li strong {color:var(--text);}
  .btn-row {display:flex;flex-wrap:wrap;gap:0.75rem;align-items:center;}
  .btn {
    background:var(--accent);color:#fff;padding:0.8rem 1.5rem;border-radius:9999px;
    font-size:0.95rem;font-weight:600;display:inline-block;transition:var(--transition);
  }
  .btn:hover {background:var(--accent-light);}
  .btn-outline {
    border:1.5px solid var(--accent);color:var(--accent-dark);padding:0.75rem 1.4rem;border-radius:9999px;
    font-size:0.95rem;font-weight:600;display:inline-block;transition:var(--transition);
  }
  .btn-outline:hover {background:var(--accent-tint);}
  .fine {font-size:0.8rem;color:var(--text-dim);margin:1rem 0 0;}
  .checks {list-style:none;display:grid;gap:0.6rem;}
  .checks li {
    background:var(--bg-card);border:1px solid var(--border);border-radius:0.9rem;
    padding:0.85rem 1rem;font-size:0.92rem;color:var(--text-muted);display:flex;gap:0.7rem;
  }
  .checks li strong {color:var(--text);}
  .tick {color:var(--accent);font-weight:800;flex-shrink:0;}
  .plan {
    background:var(--accent-tint);border-radius:var(--radius);padding:1.5rem 1.75rem;
  }
  .plan ul {margin:0 0 1.25rem 1.2rem;color:var(--text-muted);font-size:0.92rem;}
  .plan li {margin-bottom:0.4rem;}
  .price {font-size:1.6rem;font-weight:800;color:var(--text);letter-spacing:-0.02em;}
  .price span {font-size:0.9rem;font-weight:500;color:var(--text-muted);}
  .foot {text-align:center;margin-top:3rem;font-size:0.8rem;color:var(--text-dim);}
  .foot a {color:var(--accent-dark);}
  .foot a:hover {text-decoration:underline;}
  @media(max-width:600px) {
    .container {padding:2.5rem 1rem 3rem;}
    h1 {font-size:1.8rem;}
    .offer, .plan {padding:1.35rem 1.15rem;}
    .btn, .btn-outline {width:100%;text-align:center;}
    .nav {padding:1rem;}
    .nav-cta {display:none;}
    .nav-toggle { display: flex; }
    .nav-links {
      display: none; position: absolute; top: 100%; left: 0; right: 0;
      flex-direction: column; gap: 0; padding: 0.5rem 1rem 1.25rem;
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
    <img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="height:26px;width:auto;display:block;">
  </a>
  <div class="nav-links" id="navLinks">
    <a href="/tc-check">TC Check</a>
    <a href="/pricing">Pricing</a>
    <a href="/faq">FAQ</a>
    <a href="/login">Log In</a>
  </div>
  <a href="/tc-check/bulk?src=brokers_page" class="nav-cta" data-evt="brokers_audit_cta">Free Backlog Audit</a>
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
  <div class="kicker">For managing brokers</div>
  <h1>Find out what your agents leave blank &mdash; before title calls you about it.</h1>
  <p class="lede">You supervise every file, but you usually only see the broken ones when title kicks them back: <strong>a blank Effective Date, a missing initial, a 40-11 that disagrees with the contract.</strong> Run your agents' last 20 closed files through TC Check and see the pattern for yourself. Free, no signup, no sales call.</p>

  <div class="offer">
    <h2>Free backlog audit: your last 20 closed files</h2>
    <ol>
      <li><strong>Pull 20 recent closed TREC 20-19 contracts</strong> &mdash; the executed contract PDFs, one per transaction.</li>
      <li><strong>Zip them and upload</strong> &mdash; enter an email so you get a link to the results.</li>
      <li><strong>Get a batch report</strong> &mdash; how many files had at least one issue, and which issues showed up most across your roster.</li>
    </ol>
    <div class="btn-row">
      <a class="btn" href="/tc-check/bulk?src=brokers_page" data-evt="brokers_audit_cta">Start the free audit &rarr;</a>
      <a class="btn-outline" href="mailto:support@txtanoffer.com?subject=Brokerage%20backlog%20audit" data-evt="brokers_email_cta">Walk me through it</a>
    </div>
    <p class="fine">Want to check just one file first? <a href="/tc-check" style="text-decoration:underline;">Try a single file</a>. TC Check flags what's missing or inconsistent; it isn't legal advice and doesn't replace your own review.</p>
  </div>

  <h2>What the audit looks for</h2>
  <ul class="checks">
    <li><span class="tick">&check;</span><span><strong>Blank required fields</strong> &mdash; Effective Date, earnest money, escrow agent, title fields.</span></li>
    <li><span class="tick">&check;</span><span><strong>Missing initials</strong> &mdash; on every page, including the addendum.</span></li>
    <li><span class="tick">&check;</span><span><strong>40-11 vs. contract mismatches</strong> &mdash; the loan amount in the Third Party Financing Addendum disagreeing with Section 3B.</span></li>
    <li><span class="tick">&check;</span><span><strong>Financing checkbox conflicts</strong> &mdash; Section 3B and Section 22 telling title two different stories.</span></li>
  </ul>
  <p style="margin-top:1rem;">Checks are mapped field-by-field to TREC's published 20-19. <a href="/trec-changes" style="color:var(--accent-dark);text-decoration:underline;">See what changed in the current version &rarr;</a></p>

  <h2>If the audit finds a pattern</h2>
  <div class="plan">
    <div class="price">$349<span>/month for your whole roster</span></div>
    <ul>
      <li>Every agent's offer checked before it's sent</li>
      <li>Bulk-check up to 200 files per batch with your join code</li>
      <li>Contract archive: forward or drop executed contracts &mdash; checked and kept 5 years, searchable by address</li>
      <li>Roster &amp; compliance dashboard, Transaction Workspace and Closing Checklist</li>
      <li>Agents join with one text &mdash; no per-agent setup</li>
    </ul>
    <a class="btn" href="/pricing#brokerage" data-evt="brokers_plan_cta">See the Brokerage plan</a>
  </div>

  <h2>Who's behind this</h2>
  <p>TxtAnOffer is built and run in Texas by Phanel Jean Baptiste, a software builder, not a licensed agent. It's not affiliated with or endorsed by TREC. Questions go straight to me at <a href="mailto:support@txtanoffer.com" style="color:var(--accent-dark);text-decoration:underline;">support@txtanoffer.com</a>. <a href="/about" style="color:var(--accent-dark);text-decoration:underline;">The full story &rarr;</a></p>

  <p class="foot"><a href="/">&larr; Back to home</a> &middot; <a href="/pricing">Pricing</a> &middot; <a href="/faq">FAQ</a> &middot; <a href="/contact">Contact</a></p>
</div>
</body>
</html>"""
    resp = make_response(html)
    track_page_view(resp, "brokers_page")
    return resp


@app.route("/contact")
def contact():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Contact — TxtAnOffer</title>
<meta name="description" content="Get in touch with TxtAnOffer support by email.">
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
    --accent: #0b5d52;
    --accent-light: #16806e;
    --accent-dark: #0a3a33;
    --accent-tint: #E7F3F1;
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
    <img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="height:26px;width:auto;display:block;">
  </a>
  <div class="nav-links" id="navLinks">
    <a href="/#how">How it works</a>
    <a href="/pricing">Pricing</a>
    <a href="/faq">FAQ</a>
    <a href="/login">Log In</a>
  </div>
  <a href="/tc-check" class="nav-cta">Try TC Check Free</a>
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
    <p>Questions, feedback, or need a hand? Email us &mdash; a real person reads every message.</p>
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
      <div class="contact-label">Text offers (automated)</div>
      <div class="contact-value">+1 (833) 897-0333</div>
      <div style="font-size:0.8rem;color:var(--text-muted);margin-top:0.2rem;">This line drafts offers from your texts. It isn&rsquo;t read by a person &mdash; for help, use email. Reply HELP for commands.</div>
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
    --border-hover: rgba(11,93,82,0.35);
    --text: #0f1f2f;
    --text-muted: #5a6b7a;
    --text-dim: #8a9aa9;
    --accent: #0b5d52;
    --accent-light: #16806e;
    --accent-dark: #0a3a33;
    --accent-tint: #E7F3F1;
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
  .nav-cta:hover {{transform:scale(1.05);box-shadow:0 0 24px rgba(11,93,82,0.3);}}
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
  .form-card input:focus {{border-color:var(--accent);box-shadow:0 0 0 3px rgba(11,93,82,0.15);}}
  .form-card input::placeholder {{color:#b8c2ca;}}
  .row {{display:flex;gap:0.75rem;}}
  .row > div {{flex:1;}}
  .form-card button {{
    width:100%;margin-top:1.5rem;
    background:linear-gradient(135deg,var(--accent),#0a3a33);color:#fff;border:none;
    border-radius:var(--radius-sm);padding:0.85rem;font-weight:600;font-size:0.95rem;
    font-family:inherit;cursor:pointer;transition:var(--transition);
  }}
  .form-card button:hover {{transform:translateY(-2px);box-shadow:0 8px 24px rgba(11,93,82,0.25);}}
  .success {{
    margin-top:1rem;padding:0.85rem 1rem;
    background:var(--accent-tint);border:1px solid rgba(11,93,82,0.25);
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
    <img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer" style="height:26px;width:auto;display:block;">
  </a>
  <div class="nav-links" id="navLinks">
    <a href="/">Home</a>
    <a href="/demo">Demo</a>
    <a href="/pricing">Pricing</a>
  </div>
  <a href="/tc-check" class="nav-cta">Try TC Check Free</a>
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
--text:#0f1f2f;--text-muted:#5a6b7a;--text-dim:#8a9aa9;--accent:#0b5d52;--accent-light:#16806e;
--accent-dark:#0a3a33;--accent-tint:#E7F3F1;--radius:1.25rem;--radius-sm:0.85rem;}}
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;
-webkit-font-smoothing:antialiased;}}
.top-bar{{background:var(--accent-tint);border-bottom:1px solid rgba(11,93,82,0.2);
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
.btn-primary{{background:linear-gradient(135deg,var(--accent),#0a3a33);color:#fff;}}
.btn-primary:hover{{transform:translateY(-1px);box-shadow:0 6px 20px rgba(11,93,82,0.25);}}
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
.sent-banner{{background:var(--accent-tint);border:1px solid rgba(11,93,82,0.25);color:var(--accent-dark);
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
            # Day One summary: a second, immediate text with the deadlines
            # that now actually exist now that there's an Effective Date --
            # same content the /thread page shows once accepted (see
            # build_day_one_summary in deadlines.py), sent right away rather
            # than making the agent come back to read it. The T-1-day
            # earnest-money/option reminders in reminders.py cover the
            # follow-up nudge closer to each deadline.
            if action == "accept":
                effective_date = datetime.utcnow().date()
                close_date = None
                try:
                    created_dt = datetime.fromisoformat(offer["created_at"])
                    if offer.get("close_days"):
                        close_date = (created_dt + timedelta(days=offer["close_days"])).date()
                except (KeyError, TypeError, ValueError):
                    pass
                summary_lines = build_day_one_summary(
                    offer["address"], effective_date, offer.get("option_days"), close_date
                )
                twilio_send_sms(offer["phone"], "\n".join(summary_lines))
                transaction_tasks.seed_default_tasks(
                    filename, effective_date, offer.get("option_days"), close_date
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

    # Day One summary: only once there's a real Effective Date to compute
    # from (i.e. accepted) -- same content as the immediate acceptance SMS
    # (see build_day_one_summary in deadlines.py), shown here so it's still
    # available on a later visit, with a copy button so the agent can paste
    # it into an email to the buyer/lender/title company themselves (this
    # app doesn't collect those contacts, so it can't send to them directly).
    day_one_html = ""
    if thread_status in ("accept", "accepted"):
        try:
            effective_date = datetime.fromisoformat(offer.get("thread_responded_at") or "").date()
        except ValueError:
            effective_date = created_dt.date()
        close_date_obj = (created_dt + timedelta(days=close_days)).date() if close_days else None
        summary_lines = build_day_one_summary(address, effective_date, offer.get("option_days"), close_date_obj)
        summary_text = "\n".join(summary_lines)
        summary_items_html = "".join(f"<li>{line}</li>" for line in summary_lines[1:-1])
        import json as _json
        day_one_html = f"""
<div class="day-one-card">
<div class="day-one-title">Day One Summary</div>
<ul class="day-one-list">{summary_items_html}</ul>
<button class="btn btn-outline" style="width:100%;" onclick="copyDayOneSummary()">Copy summary</button>
<div class="day-one-hint">Paste this into an email or text to the buyer, lender, and title company.</div>
</div>
<script>
function copyDayOneSummary(){{navigator.clipboard.writeText({_json.dumps(summary_text)});}}
</script>"""

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
--text:#0f1f2f;--text-muted:#5a6b7a;--text-dim:#8a9aa9;--accent:#0b5d52;--accent-light:#16806e;
--accent-dark:#0a3a33;--accent-tint:#E7F3F1;--radius:1.25rem;--radius-sm:0.85rem;}}
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;
-webkit-font-smoothing:antialiased;}}
.top-bar{{background:var(--accent-tint);border-bottom:1px solid rgba(11,93,82,0.2);
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
.btn-primary{{background:linear-gradient(135deg,var(--accent),#0a3a33);color:#fff;}}
.btn-primary:hover{{transform:translateY(-1px);box-shadow:0 6px 20px rgba(11,93,82,0.25);}}
.btn-outline{{background:transparent;color:var(--text-muted);border:1px solid var(--border);}}
.btn-outline:hover{{border-color:var(--accent);color:var(--accent-dark);}}
.pdf-frame{{width:100%;height:70vh;border:1px solid var(--border);border-radius:var(--radius-sm);
background:#f1f5f9;}}
.disclaimer{{font-size:0.75rem;color:var(--text-dim);text-align:center;padding:1rem;
border-top:1px solid var(--border);margin-top:1rem;}}
.notbinding{{font-size:0.78rem;color:var(--text-muted);background:var(--bg-card);border:1px solid var(--border);
border-radius:var(--radius-sm);padding:0.75rem 1rem;margin-bottom:1.25rem;line-height:1.5;}}
.status-panel{{background:var(--accent-tint);border:1px solid rgba(11,93,82,0.25);color:var(--accent-dark);
border-radius:var(--radius-sm);padding:0.9rem 1rem;text-align:center;font-size:0.9rem;margin-bottom:1.25rem;}}
.recipient-cta{{background:var(--accent-tint);border:1px solid rgba(11,93,82,0.2);border-radius:var(--radius-sm);
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
.day-one-card{{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-sm);
padding:1.1rem 1.25rem;margin-bottom:1.25rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);}}
.day-one-title{{font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;
color:var(--text-dim);margin-bottom:0.7rem;}}
.day-one-list{{list-style:none;margin-bottom:0.9rem;}}
.day-one-list li{{font-size:0.88rem;color:var(--text);padding:0.35rem 0;border-bottom:1px solid var(--border);}}
.day-one-list li:last-child{{border-bottom:none;}}
.day-one-hint{{font-size:0.75rem;color:var(--text-dim);text-align:center;margin-top:0.6rem;}}
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

{day_one_html}

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


@app.route("/transaction/<path:filename>", methods=["GET", "POST"])
def transaction_page(filename):
    """TC-only transaction workspace for one accepted offer: a fixed-stage
    timeline, a per-stage task checklist, and (within 5 days of closing) a
    Closing Checklist panel. Gated by the same signed expires/sig scheme as
    /review and /offers -- this link is minted by sign_transaction_url() and
    handed out on the dashboard, never on the public /thread page.

    GET renders the page. POST is a JSON API (toggle/add/delete a task) used
    by the page's own fetch() calls -- no full reload on a checkbox click."""
    if ".." in filename or filename.startswith("/"):
        abort(400)
    expires = request.values.get("expires")
    sig = request.values.get("sig")
    if not verify_pdf_signature(filename, expires, sig):
        abort(403)

    offer = get_offer_by_filename(filename)
    if not offer or not offer["price"]:
        abort(404)

    thread_status = offer.get("thread_status") or "pending"

    if request.method == "POST":
        client_ip = request.remote_addr or "unknown"
        if not check_and_increment(f"tx_task:{client_ip}", limit=120):
            return jsonify({"error": "Too many requests. Try again in a bit."}), 429
        if thread_status not in ("accept", "accepted"):
            return jsonify({"error": "This offer hasn't been accepted yet."}), 400
        if not has_professional_access(offer["phone"]):
            return jsonify({"error": "The Transaction Workspace is a Professional-plan feature. Upgrade at txtanoffer.com/pricing."}), 403
        data = request.get_json(silent=True) or {}
        action = data.get("action", "")
        if action == "toggle":
            task_id = data.get("task_id")
            ok = transaction_tasks.toggle_task(filename, int(task_id), bool(data.get("done")))
            if not ok:
                return jsonify({"error": "Task not found"}), 404
        elif action == "add":
            stage = (data.get("stage") or "").strip()
            label = (data.get("label") or "").strip()
            due_date = (data.get("due_date") or "").strip()
            if not label or stage not in transaction_tasks.STAGES:
                return jsonify({"error": "Stage and label required"}), 400
            transaction_tasks.add_task(filename, stage, label, due_date)
        elif action == "delete":
            task_id = data.get("task_id")
            transaction_tasks.delete_task(filename, int(task_id))
        else:
            return jsonify({"error": "Unknown action"}), 400
        return jsonify({"ok": True, "tasks": transaction_tasks.get_tasks(filename)})

    track_event("transaction_viewed", offer["phone"], {"filename": filename})

    address = offer["address"]
    price = offer["price"]
    close_days = offer["close_days"]
    try:
        created_dt = datetime.fromisoformat(offer["created_at"])
    except (KeyError, TypeError, ValueError):
        created_dt = datetime.now()
    close_date_obj = (created_dt + timedelta(days=close_days)).date() if close_days else None

    if thread_status not in ("accept", "accepted"):
        return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Transaction — {address}</title>
<style>body{{font-family:'Inter',sans-serif;background:#F5F5F7;color:#0f1f2f;min-height:100vh;
display:flex;align-items:center;justify-content:center;text-align:center;padding:2rem;}}
.box{{max-width:420px;}}h2{{margin-bottom:0.75rem;}}p{{color:#5a6b7a;font-size:0.9rem;line-height:1.6;}}</style>
</head><body><div class="box"><h2>Not accepted yet</h2>
<p>The Transaction workspace unlocks once the listing agent accepts this offer on its Offer Thread page. Nothing to track until then.</p></div></body></html>"""

    if not has_professional_access(offer["phone"]):
        return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Transaction — {address}</title>
<style>body{{font-family:'Inter',sans-serif;background:#F5F5F7;color:#0f1f2f;min-height:100vh;
display:flex;align-items:center;justify-content:center;text-align:center;padding:2rem;}}
.box{{max-width:440px;}}h2{{margin-bottom:0.75rem;}}p{{color:#5a6b7a;font-size:0.9rem;line-height:1.6;margin-bottom:1.25rem;}}
a.btn{{display:inline-block;background:#0b5d52;color:#fff;padding:0.75rem 1.5rem;border-radius:999px;font-size:0.9rem;font-weight:600;text-decoration:none;}}</style>
</head><body><div class="box"><h2>Transaction Workspace is part of the Brokerage plan</h2>
<p>This offer was accepted &mdash; the stage timeline, task checklist, and Closing Checklist for {address} are ready once your brokerage is on the Brokerage plan. Individual agent? Email support@txtanoffer.com and we&rsquo;ll set it up for you.</p>
<a href="/pricing#brokerage" class="btn">See the Brokerage plan &rarr;</a></div></body></html>"""

    try:
        effective_date = datetime.fromisoformat(offer.get("thread_responded_at") or "").date()
    except ValueError:
        effective_date = created_dt.date()

    transaction_tasks.seed_default_tasks(filename, effective_date, offer.get("option_days"), close_date_obj)
    tasks = transaction_tasks.get_tasks(filename)

    tasks_by_stage = {s: [] for s in transaction_tasks.STAGES}
    for t in tasks:
        tasks_by_stage.setdefault(t["stage"], []).append(t)

    today = datetime.utcnow().date()

    def _parse_due(t):
        try:
            return datetime.fromisoformat(t["due_date"]).date() if t["due_date"] else None
        except ValueError:
            return None

    due_this_week = [t for t in tasks if not t["done"] and _parse_due(t) and _parse_due(t) <= today + timedelta(days=7)]
    open_count = len([t for t in tasks if not t["done"]])

    # Stage state: "done" once every one of its tasks is checked (a stage
    # with zero tasks -- e.g. the TC deleted them all -- reads as done too,
    # nothing left to do there). "current" is the first not-done stage in
    # STAGES order; everything after it is "upcoming".
    stage_state = {}
    seen_current = False
    for s in transaction_tasks.STAGES:
        stage_tasks = tasks_by_stage.get(s, [])
        is_done = all(t["done"] for t in stage_tasks) if stage_tasks else True
        if is_done:
            stage_state[s] = "done"
        elif not seen_current:
            stage_state[s] = "current"
            seen_current = True
        else:
            stage_state[s] = "upcoming"

    timeline_html = ""
    for s in transaction_tasks.STAGES:
        st = stage_state[s]
        timeline_html += f'<div class="tl-node tl-{st}"><div class="tl-dot"></div><div class="tl-label">{s}</div></div>'

    def _fmt(d):
        return d.strftime("%b %d, %Y") if d else "—"

    import json as _json

    stage_sections_html = ""
    for s in transaction_tasks.STAGES:
        stage_tasks = tasks_by_stage.get(s, [])
        rows = ""
        for t in stage_tasks:
            due = _parse_due(t)
            due_badge = ""
            if due and not t["done"]:
                overdue = due < today
                due_badge = f'<span class="task-due {"overdue" if overdue else ""}">{"Overdue " if overdue else "Due "}{due.strftime("%b %d")}</span>'
            rows += f"""
            <div class="task-row" data-id="{t['id']}">
              <label class="task-check">
                <input type="checkbox" {"checked" if t['done'] else ""} onchange="toggleTask({t['id']}, this.checked)">
                <span class="{'task-label done' if t['done'] else 'task-label'}">{t['label']}</span>
              </label>
              {due_badge}
              <button class="task-del" onclick="deleteTask({t['id']})" title="Remove">&times;</button>
            </div>"""
        stage_sections_html += f"""
        <div class="stage-section">
          <div class="stage-head"><span class="stage-badge stage-{stage_state[s]}">{stage_state[s]}</span><span class="stage-name">{s}</span></div>
          {rows or '<div class="stage-empty">No tasks.</div>'}
          <form class="add-task-form" onsubmit="return addTask(event, {_json.dumps(s)})">
            <input type="text" placeholder="Add a task..." maxlength="200" required>
            <input type="date" title="Due date (optional)">
            <button type="submit">+</button>
          </form>
        </div>"""

    # Closing Checklist: only surfaces within 5 days of the closing date
    # printed on the contract -- every signal in it is real, pulled fresh,
    # never inferred. "Sent for signature" is exactly that, not "signed":
    # this app doesn't poll DocuSign for envelope completion.
    closing_html = ""
    if close_date_obj and 0 <= (close_date_obj - today).days <= 5:
        days_out = (close_date_obj - today).days
        pdf_path_on_disk = os.path.join(OUTPUT_DIR, filename)
        doc_status_html = "Not available"
        if os.path.exists(pdf_path_on_disk):
            try:
                audit = check_tc_file([pdf_path_on_disk])
                if audit.get("complete"):
                    doc_status_html = '<span class="ok">Complete &mdash; no missing fields</span>'
                else:
                    issues = audit.get("issues") or []
                    doc_status_html = f'<span class="warn">{len(issues)} open issue{"s" if len(issues) != 1 else ""}</span>'
            except Exception:
                doc_status_html = "Not available"
        if offer.get("docusign_sent_at"):
            sig_status_html = f'<span class="ok">Sent for signature {offer["docusign_sent_at"][:10]}</span>'
        else:
            sig_status_html = '<span class="warn">Not yet sent via DocuSign</span>'
        earnest_dl = earnest_money_deadline(effective_date)
        opt_end = option_end_date(effective_date, offer.get("option_days"))
        closing_html = f"""
        <div class="closing-card">
          <div class="closing-title">Closing in {days_out} day{'s' if days_out != 1 else ''}</div>
          <div class="closing-row"><span>Documents</span>{doc_status_html}</div>
          <div class="closing-row"><span>Signatures</span>{sig_status_html}</div>
          <div class="closing-row"><span>Outstanding tasks</span><span class="{'warn' if open_count else 'ok'}">{open_count} open</span></div>
          <div class="closing-dates">
            <div>Effective Date: {_fmt(effective_date)}</div>
            <div>Earnest money due: {_fmt(earnest_dl)}</div>
            {f'<div>Option period ended: {_fmt(opt_end)}</div>' if opt_end else ''}
            <div>Closing: {_fmt(close_date_obj)}</div>
          </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Transaction — {address}</title>
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
:root{{--bg:#F5F5F7;--bg-card:#fff;--border:rgba(15,31,47,0.08);
--text:#0f1f2f;--text-muted:#5a6b7a;--text-dim:#8a9aa9;--accent:#0b5d52;--accent-light:#16806e;
--accent-dark:#0a3a33;--accent-tint:#E7F3F1;--radius:1.25rem;--radius-sm:0.85rem;}}
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;-webkit-font-smoothing:antialiased;}}
.container{{max-width:640px;margin:0 auto;padding:1.5rem 1rem 3rem;}}
.address-card{{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
padding:1.25rem 1.5rem;text-align:center;margin-bottom:1.25rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);}}
.address-card h1{{font-size:1.2rem;font-weight:700;}}
.address-card .meta{{color:var(--text-dim);font-size:0.8rem;margin-top:0.15rem;}}
.week-banner{{background:var(--accent-tint);border:1px solid rgba(11,93,82,0.2);border-radius:var(--radius-sm);
padding:0.75rem 1rem;text-align:center;font-size:0.85rem;font-weight:600;color:var(--accent-dark);margin-bottom:1.25rem;}}
.timeline{{display:flex;overflow-x:auto;gap:0;background:var(--bg-card);border:1px solid var(--border);
border-radius:var(--radius-sm);padding:1.1rem 0.75rem;margin-bottom:1.25rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);}}
.tl-node{{flex:0 0 auto;width:5.6rem;text-align:center;position:relative;}}
.tl-dot{{width:14px;height:14px;border-radius:50%;background:var(--border);margin:0 auto 0.4rem;border:2px solid var(--bg-card);box-shadow:0 0 0 2px var(--border);}}
.tl-node::before{{content:'';position:absolute;top:6px;left:-50%;width:100%;height:2px;background:var(--border);z-index:0;}}
.tl-node:first-child::before{{display:none;}}
.tl-done .tl-dot{{background:var(--accent);box-shadow:0 0 0 2px var(--accent);}}
.tl-done::before{{background:var(--accent);}}
.tl-current .tl-dot{{background:#fff;box-shadow:0 0 0 2px var(--accent);}}
.tl-label{{font-size:0.62rem;font-weight:600;color:var(--text-dim);line-height:1.2;}}
.tl-done .tl-label,.tl-current .tl-label{{color:var(--text);}}
.closing-card{{background:var(--bg-card);border:2px solid var(--accent);border-radius:var(--radius-sm);
padding:1.1rem 1.25rem;margin-bottom:1.25rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);}}
.closing-title{{font-size:1rem;font-weight:700;color:var(--accent-dark);margin-bottom:0.75rem;}}
.closing-row{{display:flex;justify-content:space-between;align-items:center;font-size:0.85rem;padding:0.35rem 0;border-bottom:1px solid var(--border);}}
.closing-row:last-of-type{{border-bottom:none;}}
.closing-dates{{margin-top:0.75rem;padding-top:0.6rem;border-top:1px solid var(--border);font-size:0.78rem;color:var(--text-muted);line-height:1.7;}}
.ok{{color:var(--accent-dark);font-weight:600;}}
.warn{{color:#b45309;font-weight:600;}}
.stage-section{{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-sm);
padding:1rem 1.1rem;margin-bottom:0.85rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);}}
.stage-head{{display:flex;align-items:center;gap:0.5rem;margin-bottom:0.6rem;}}
.stage-name{{font-weight:700;font-size:0.92rem;}}
.stage-badge{{font-size:0.62rem;font-weight:700;text-transform:uppercase;letter-spacing:0.04em;padding:0.15rem 0.5rem;border-radius:999px;}}
.stage-done{{background:var(--accent-tint);color:var(--accent-dark);}}
.stage-current{{background:rgba(245,158,11,0.15);color:#b45309;}}
.stage-upcoming{{background:var(--bg);color:var(--text-dim);}}
.stage-empty{{font-size:0.8rem;color:var(--text-dim);padding:0.3rem 0;}}
.task-row{{display:flex;align-items:center;gap:0.5rem;padding:0.4rem 0;border-bottom:1px solid var(--border);}}
.task-row:last-of-type{{border-bottom:none;}}
.task-check{{display:flex;align-items:center;gap:0.6rem;flex:1;cursor:pointer;font-size:0.85rem;}}
.task-check input{{width:17px;height:17px;accent-color:var(--accent);flex-shrink:0;}}
.task-label.done{{text-decoration:line-through;color:var(--text-dim);}}
.task-due{{font-size:0.68rem;font-weight:600;color:var(--text-dim);white-space:nowrap;}}
.task-due.overdue{{color:#dc2626;}}
.task-del{{background:none;border:none;color:var(--text-dim);font-size:1.1rem;cursor:pointer;padding:0 0.25rem;line-height:1;}}
.task-del:hover{{color:#dc2626;}}
.add-task-form{{display:flex;gap:0.4rem;margin-top:0.6rem;}}
.add-task-form input[type=text]{{flex:1;min-width:0;padding:0.45rem 0.6rem;border:1px solid var(--border);border-radius:0.5rem;font-family:inherit;font-size:0.8rem;}}
.add-task-form input[type=date]{{padding:0.45rem 0.4rem;border:1px solid var(--border);border-radius:0.5rem;font-family:inherit;font-size:0.75rem;width:8.5rem;}}
.add-task-form button{{background:var(--accent);color:#fff;border:none;border-radius:0.5rem;width:2.2rem;font-size:1rem;cursor:pointer;}}
@media(max-width:400px){{.add-task-form input[type=date]{{width:6.2rem;}}}}
.back-link{{display:block;text-align:center;font-size:0.8rem;color:var(--text-dim);margin-top:1.5rem;text-decoration:none;}}
</style>
</head>
<body>
<div class="container">
<div class="address-card"><h1>{address}</h1><div class="meta">${price:,} &middot; Transaction Workspace</div></div>
{f'<div class="week-banner">{len(due_this_week)} task{"s" if len(due_this_week) != 1 else ""} due this week</div>' if due_this_week else ''}
<div class="timeline">{timeline_html}</div>
{closing_html}
{stage_sections_html}
<a href="/dashboard" class="back-link">&larr; Back to dashboard</a>
</div>
<script>
function toggleTask(id, done){{
  fetch(location.pathname + location.search, {{method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{action:'toggle', task_id:id, done:done}})}}).then(()=>location.reload());
}}
function deleteTask(id){{
  fetch(location.pathname + location.search, {{method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{action:'delete', task_id:id}})}}).then(()=>location.reload());
}}
function addTask(ev, stage){{
  ev.preventDefault();
  var inputs = ev.target.querySelectorAll('input');
  var label = inputs[0].value, due = inputs[1].value;
  if(!label.trim()) return false;
  fetch(location.pathname + location.search, {{method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{action:'add', stage:stage, label:label, due_date:due}})}}).then(()=>location.reload());
  return false;
}}
</script>
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
    # Same rule as brokerages.get_retained_brokerage_filenames(): paying,
    # admin, and brokerage-linked agents' offers are kept 5 years; free-trial
    # offers 30 days (cleanup.py). Shown on each card so nothing vanishes silently.
    archived_plan = bool(user.get("is_subscribed")) or is_admin_phone(phone) or bool(user.get("brokerage_id"))

    # Build offer cards, with each offer's amendments nested inside the same card
    offer_cards = ""
    accepted_volume = 0
    accepted_count = 0
    for o in offers:
        pdf_link = sign_pdf_url(o["filename"], request.host_url.rstrip("/"))
        created = o["created_at"][:10]
        pdf_exists = bool(o.get("filename")) and os.path.isfile(os.path.join(OUTPUT_DIR, o["filename"]))

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

        if not pdf_exists:
            keep_html = '<div class="keep-line gone">PDF no longer stored'+ (' (free-trial PDFs are kept 30 days)' if not archived_plan else '') +'</div>'
        elif archived_plan:
            keep_html = f'<div class="keep-line">&#10003; Saved in your archive &middot; kept until {(created_dt + timedelta(days=5*365)).strftime("%b %Y")}</div>'
        else:
            keep_html = f'<div class="keep-line trial">Free trial &middot; PDF kept until {(created_dt + timedelta(days=30)).strftime("%b %-d")} &middot; <a href="/pricing">keep it 5 years</a></div>'

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
                <div class="offer-addr">{escape(o['address'] or '')}</div>
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
            {keep_html}
            {f'<a href="{pdf_link}" target="_blank" class="btn-primary">View PDF</a>' if pdf_exists else '<span class="btn-primary disabled">PDF expired</span>'}
            {f'<a href="{sign_transaction_url(o["filename"], request.host_url.rstrip("/"))}" target="_blank" class="btn-primary" style="margin-top:0.5rem;background:linear-gradient(135deg,var(--accent),#0a3a33);">Transaction &rarr;</a>' if status == "accepted" else ''}
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
      <div class="milestone-logo"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="#0a3f3a" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg></div>
      <div class="milestone-val">Congrats on ${accepted_volume:,}!</div>
      <div class="milestone-sub">{accepted_count} offer{'s' if accepted_count != 1 else ''} accepted through TxtAnOffer</div>
      <a href="{wins_url}" target="_blank" class="milestone-share">Share your milestone &rarr;</a>"""
    else:
        milestone_html = """
      <div class="milestone-logo"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="#0a3f3a" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg></div>
      <div class="milestone-sub">Your first accepted offer will show up here.</div>"""

    initials = "".join(part[0] for part in agent.get("name", "").split()[:2]).upper() if agent.get("name") else "?"

    if is_admin_phone(phone):
        sub_status = "Admin (Unlimited)"
        sub_badge_color = "#FFF6DA"
        sub_badge_text = "#8a5a00"
    elif user["is_subscribed"]:
        sub_status = "Active"
        sub_badge_color = "var(--accent-tint)"
        sub_badge_text = "#000000"
    else:
        sub_status = f"Free ({user['offer_count']}/{FREE_OFFER_LIMIT} used)"
        sub_badge_color = "rgba(245,158,11,0.12)"
        sub_badge_text = "#b45309"

    profile_url = f"/profile?phone={_urlquote(phone, safe='')}&expires={expires}&sig={sig}"
    archive_sub = ("Every offer you text in is kept 5 years &mdash; open any PDF here anytime." if archived_plan else
                   'On the free trial, offer PDFs are kept 30 days. <a href="/pricing" style="color:#0b5d52;font-weight:700;">Keep them 5 years &rarr;</a>')

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
    --border-hover: rgba(11,93,82,0.35);
    --text: #0f1f2f;
    --text-muted: #5a6b7a;
    --text-dim: #8a9aa9;
    --accent: #0b5d52;
    --accent-light: #16806e;
    --accent-dark: #0a3a33;
    --accent-tint: #E7F3F1;
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
    background:#0a3f3a;border-bottom:4px solid #f5c242;z-index:100;
  }}
  .nav-left {{display:flex;align-items:center;gap:0.6rem;font-weight:700;font-size:1.1rem;letter-spacing:-0.02em;color:var(--text);}}
  .nav-logo {{width:34px;height:34px;border-radius:22%;overflow:hidden;}}
  .nav-logo img {{width:100%;height:100%;object-fit:contain;}}
  .nav-links {{display:flex;gap:2rem;font-size:0.875rem;font-weight:600;color:#c9dcd8;}}
  .nav-links a {{transition:var(--transition);}}
  .nav-links a:hover {{color:#f5c242;}}
  .nav-toggle {{ display: none; flex-direction: column; justify-content: center; gap: 5px; width: 34px; height: 34px; background: none; border: none; cursor: pointer; padding: 0; }}
  .nav-toggle span {{ display: block; width: 100%; height: 2px; background: #fff; border-radius: 2px; }}

  .container {{max-width:1000px;margin:0 auto;padding:2.5rem 2rem 4rem;}}
  .greeting {{font-size:1.75rem;font-weight:800;letter-spacing:-0.03em;margin-bottom:0.5rem;color:var(--text);}}
  .sub-badge {{
    display:inline-block;background:{sub_badge_color};color:{sub_badge_text};
    padding:0.3rem 0.85rem;border-radius:9999px;font-size:0.75rem;font-weight:700;
    letter-spacing:0.02em;
  }}

  .milestone-card {{
    text-align:center;margin-top:1.5rem;border-radius:var(--radius);
    background:#0a3f3a;padding:2.25rem 2rem;color:#fff;
  }}
  .milestone-logo {{width:48px;height:48px;margin:0 auto 1.1rem;border-radius:50%;background:#f5c242;
    display:flex;align-items:center;justify-content:center;}}
  .milestone-val {{font-size:2.1rem;font-weight:800;letter-spacing:-0.02em;line-height:1.1;color:#f5c242;}}
  .milestone-sub {{font-size:0.88rem;color:#c9dcd8;margin-top:0.5rem;}}
  .milestone-share {{
    display:inline-block;margin-top:1.4rem;font-size:0.85rem;font-weight:700;
    color:#0f1f2f;background:#f5c242;border-radius:999px;padding:0.55rem 1.1rem;
    transition:var(--transition);
  }}
  .milestone-share:hover {{background:#ffd25e;}}

  .stats {{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1rem;margin:2rem 0;}}
  .stat {{
    background:var(--bg-card);
    border:1px solid var(--border);border-top:4px solid #f5c242;border-radius:var(--radius);
    padding:1.4rem 1.5rem;transition:var(--transition);
    box-shadow:0 1px 3px rgba(15,31,47,0.05);
  }}
  .stat:hover {{border-color:var(--border-hover);transform:translateY(-1px);}}
  .stat-val {{font-size:1.75rem;font-weight:800;color:var(--accent);}}
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
    background:#f5c242;
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
  .offer-card-bar.status-draft {{background:linear-gradient(180deg, #0b5d52, #16806e);}}
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
  .status-badge.status-draft {{background:rgba(11,93,82,0.12);color:#0a3f3a;}}
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
    box-shadow:0 2px 10px rgba(11,93,82,0.25);transition:var(--transition);
  }}
  .btn-primary:hover {{background:var(--accent-dark);}}
  .btn-primary.disabled {{background:#e5e8eb;color:#8a9aa9;box-shadow:none;cursor:default;}}
  .keep-line {{font-size:0.78rem;font-weight:600;color:var(--accent);margin:-0.3rem 0 0.8rem;}}
  .keep-line.trial {{color:#b45309;font-weight:500;}}
  .keep-line.trial a {{color:#0b5d52;font-weight:700;text-decoration:underline;}}
  .keep-line.gone {{color:var(--text-dim);font-weight:500;}}
  .section-sub {{font-size:0.85rem;color:var(--text-muted);margin:-0.6rem 0 1rem;}}
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
  .nav-item:hover, .nav-item.active {{color:var(--accent);}}
  .nav-item.active {{position:relative;}}
  .nav-item.active::before {{content:"";position:absolute;top:-0.7rem;left:20%;right:20%;height:3px;border-radius:2px;background:#f5c242;}}

  @media(max-width:600px){{
    .container {{padding:1.5rem 1rem 1rem;}}
    .stats {{grid-template-columns:1fr 1fr 1fr;}}
    .greeting {{font-size:1.35rem;}}
    .nav-toggle {{ display: flex; }}
    .nav-links {{
      display: none; position: absolute; top: 100%; left: 0; right: 0;
      flex-direction: column; gap: 0; padding: 0.5rem 1.25rem 1.25rem;
      background: #0a3f3a; border-bottom: 4px solid #f5c242;
    }}
    .nav-links.open {{ display: flex; }}
    .nav-links a {{ padding: 0.75rem 0; border-bottom: 1px solid rgba(255,255,255,0.12); }}
    .nav-links a:last-child {{ border-bottom: none; }}
    .offer-top {{flex-direction:column;align-items:flex-start;gap:0.15rem;}}
  }}
</style>
</head>
<body>
<nav class="nav">
  <a href="/" class="nav-left">
    <img src="/static/logo-wordmark-white.png?v=1" alt="TxtAnOffer" style="height:24px;width:auto;display:block;">
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

  <h2>Your offer archive</h2>
  <p class="section-sub">{archive_sub}</p>
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
    border-bottom:1px solid rgba(11,93,82,0.35); padding-bottom:0.1rem;
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


# Public pages search engines should index. Signed/private routes (/review,
# /offers, /transaction, /broker/dashboard, /admin, /analytics) are left out
# on purpose and disallowed in robots.txt.
_SITEMAP_PATHS = ["/", "/tc-check", "/brokers", "/pricing", "/trec-changes", "/tc-hub",
                  "/tc-check/bulk", "/tc-check/compare", "/playground", "/faq", "/about",
                  "/contact", "/privacy", "/terms", "/guides",
                  "/guides/trec-20-19-checklist", "/guides/trec-20-19-effective-date",
                  "/guides/broker-record-retention-texas", "/guides/40-11-loan-amount-mismatch",
                  "/guides/trec-20-19-initials", "/guides/trec-39-11-amendment-mismatch",
                  "/guides/trec-deadline-calculator", "/archive"]


@app.route("/robots.txt")
def robots_txt():
    body = (
        "User-agent: *\n"
        "Disallow: /admin\n"
        "Disallow: /analytics\n"
        "Disallow: /broker/dashboard/\n"
        "Disallow: /review/\n"
        "Disallow: /offers/\n"
        "Disallow: /transaction/\n"
        "Disallow: /thread/\n"
        "Disallow: /dashboard\n"
        "Disallow: /profile\n"
        "Sitemap: https://txtanoffer.com/sitemap.xml\n"
    )
    return Response(body, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    urls = "".join(f"<url><loc>https://txtanoffer.com{p}</loc></url>" for p in _SITEMAP_PATHS)
    return Response('<?xml version="1.0" encoding="UTF-8"?>'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + urls + '</urlset>',
                    mimetype="application/xml")


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
