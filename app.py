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
from amendment import fill_amendment_pdf, amended_closing_line
from agent_profiles import get_agent_profile, save_agent_profile, find_by_email, get_emails_for_phones
from subscriptions import can_generate_offer, increment_offer_count, activate_subscription, deactivate_subscription, get_user, create_user, FREE_OFFER_LIMIT, is_admin_phone, has_professional_access
from analytics import _is_internal_traffic
from analytics import track_event, get_conversion_metrics, get_revenue_metrics, get_recent_sms, get_recent_sms_failures, get_last_blocked_state, get_waitlist_signups, get_signups_by_source, get_signup_details, get_landing_visits_by_source, get_tc_check_summary, get_recent_tc_check_email_senders, get_tc_check_count_for_sender, get_tc_check_repeat_senders, get_tc_check_bulk_summary, get_brokerage_alert_delivery, get_daily_funnel, get_top_referrers, set_internal_visitor, get_engagement_by_device, get_recent_email_send_failures, get_recent_visitors, get_co_tool_usage, get_headline
from analytics import get_engagement_by_source, CHECK_CTA_EVENTS, SAMPLE_CTA_EVENTS, BOOK_CALL_EVENTS, DROPZONE_INTERACT_EVENT
from integrations import send_offer_email, fire_webhook, save_webhook, get_webhook, delete_webhook, send_to_docusign, send_plain_email, send_html_email
from offers_db import record_offer, get_offers_for_phone, get_offer_by_filename, record_amendment, get_amendments_for_phone, record_thread_response, record_email_sent, record_docusign_sent
from brokerages import extract_brokerage_prefix, link_user_to_brokerage, get_brokerage, get_brokerage_by_code, create_brokerage, list_brokerages, list_brokerage_agents, list_brokerage_records
from sponsors import create_sponsor, list_sponsors, set_sponsor_active
from sms_utils import parse_incoming_sms
from cleanup import run_cleanup_if_due, encrypt_legacy_files_in_background
import archive as archive_store
import filecrypt
import google_auth
import text_demo
filecrypt.startup_check()
encrypt_legacy_files_in_background(OUTPUT_DIR)
import crm
import broker_auth
from reminders import run_reminders_if_due
import nurture
from deadlines import earnest_money_deadline, option_end_date, fmt_option_end, build_day_one_summary, offer_closing_date, fmt_closing_date, closing_roll_note, roll_note, colorado_date
import transaction_tasks
from drafts import save_draft, get_draft, clear_draft
from tc_audit import check_tc_file, compare_contracts
import first_screen
import trec_forms
from tc_report_pdf import generate_tc_report_pdf
from rate_limit import check_and_increment
from real_upload_alert import maybe_send_alert as maybe_send_real_upload_alert
from real_upload_alert import maybe_send_unreadable_alert
from tc_gate import get_client as get_tc_client, record_use as record_tc_use, save_email as save_tc_email
from tc_nudge import run_followup_if_due as run_tc_followup_if_due
from tc_check_email import (
    extract_sender_email, extract_pdf_attachments, junk_sender_reason,
    format_reply_body, format_reply_html, subject_line,
    format_no_pdf_reply, format_no_pdf_html,
    format_unreadable_reply, format_unreadable_html,
    hand_review_email, reply_to_for, format_hand_review_request,
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

# Search Console (2026-10-11) flagged "Duplicate without user-selected
# canonical": www.txtanoffer.com served a full copy of the site, and every
# ?src= tracking link is its own URL to Google. Only the guide pages carried
# a canonical tag. GET/HEAD on www get a 301 to the apex; other methods are
# left alone so a webhook configured against www keeps working.
CANONICAL_HOST = "txtanoffer.com"


@app.before_request
def _redirect_www():
    if request.host.lower() == "www." + CANONICAL_HOST and request.method in ("GET", "HEAD"):
        qs = request.query_string.decode("utf-8", "replace")
        return redirect(f"https://{CANONICAL_HOST}{request.path}" + (f"?{qs}" if qs else ""), code=301)


@app.before_request
def _nurture_tick():
    # SMS nurture (nurture.py) rides on any request, not just /sms: with a
    # handful of texting users, /sms alone could go days without a hit and
    # the ~24h follow-ups would never fire. A no-op unless
    # SMS_NURTURE_ENABLED is set; otherwise one time check per request.
    nurture.run_nurture_if_due(twilio_send_sms)


@app.before_request
def _cleanup_tick():
    # Retention promises (30-day offer PDFs, ~90-day logs, 5-year archive
    # purge -- privacy policy and /trust) used to run only from /sms, so a
    # site with few texts never kept them. run_cleanup_if_due throttles
    # itself to once per 6h and works in a background thread.
    run_cleanup_if_due(OUTPUT_DIR)


# One shared rule for the nav CTA, injected after each page's own <style>
# so it wins the cascade. There are ~17 hand-copied navs in this file with
# their own .nav-cta CSS; some let the pill wrap to two lines at phone width
# and butt into the wordmark/hamburger, some hid it entirely under 480px.
# Until the navs are unified (Phanel's call), this keeps every copy on one
# line and visible, sized to fit next to the logo and menu button at 390px.
NAV_CTA_CSS = """<style id="nav-cta-shared">
.nav-cta{white-space:nowrap;flex-shrink:0}
@media(max-width:480px){nav.nav{padding-left:1rem;padding-right:1rem}
.nav-cta{display:inline-block;font-size:.8rem;padding:.5rem .85rem;margin-left:auto}
.nav-cta + .nav-toggle,.nav-cta + button{margin-left:.6rem}}
</style>"""


@app.after_request
def _add_canonical(resp):
    # Self-referencing canonical without the query string, so ?src=... links
    # collapse onto the clean URL. Pages that already set one (the guides)
    # are left untouched.
    if (request.method == "GET" and resp.status_code == 200 and resp.mimetype == "text/html"
            and not resp.direct_passthrough):
        body = resp.get_data(as_text=True)
        if 'class="nav-cta"' in body and "</head>" in body and 'id="nav-cta-shared"' not in body:
            body = body.replace("</head>", NAV_CTA_CSS + "\n</head>", 1)
            resp.set_data(body)
        if "</head>" in body and 'rel="canonical"' not in body:
            tag = f'<link rel="canonical" href="https://{CANONICAL_HOST}{request.path}">'
            resp.set_data(body.replace("</head>", tag + "\n</head>", 1))
    return resp

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
# workflow_offer_sms and footer_* were tagged in the HTML but missing here,
# so /v1/evt dropped them (found 2026-10-13 by tests/test_tracking.py, which
# now fails on any data-evt not in this list).
# role_shown / role_dismissed / role_<tc|agent|broker|investor|browsing>
# ("What brings you here?" card, removed 2026-10-09, 0 of 14 answered) were
# dropped from this list 2026-10-14: nothing on the site sends them anymore
# and their /analytics panel is gone. Past rows stay in the events table.
_ENGAGEMENT_TYPES = ("js_ok", "upload_picked", "upload_failed_rejected", "upload_failed_network",
                     "upload_failed_timeout", "upload_failed_server", "hero_cta", "hero_sample", "hero_card", "dropzone_seen", "stay_10s", "stay_60s",
                     "brokers_audit_cta", "brokers_email_cta", "brokers_plan_cta", "records_brokers_cta", "guide_check_cta", "guide_brokers_cta", "path_self_cta", "path_walkthrough_cta", "tc_trust_link", "tc_own_file_cta", "path_brokerage_cta", "guide_card_cta", "support_email_cta", "pricing_audit_cta", "tc_copy_agent_msg", "state_toggle_tx", "state_toggle_co", "states_tx_check", "states_tx_hub", "states_co_check", "states_co_hub", "states_co_tool", "states_request", "workflow_offer_sms", "footer_instagram", "footer_linkedin",
                     # path_walkthrough_cta / brokers_email_cta were the mailto
                     # "call" buttons until 2026-10-14; kept so old rows still parse.
                     # /tools/colorado-deadlines, 2026-10-13 (tool_check_cta is in CHECK_CTA_EVENTS)
                     "tool_calc_run", "tool_ics", "tool_copy_text", "tool_embed_copy", "hub_tool_link",
                     "guide_trec_official",
                     # first_screen.py, 2026-10-23
                     "hero_broker_cta", "tc_title_copy_link",
                     # homepage Text an offer -> /text-an-offer, 2026-10-24
                     "text_learn_more") \
    + CHECK_CTA_EVENTS + SAMPLE_CTA_EVENTS + BOOK_CALL_EVENTS + (DROPZONE_INTERACT_EVENT,)
# Pages whose beacons /v1/evt accepts: the ones with their own
# track_page_view() call, plus _BEACON_ONLY_PAGES below.
_TRACKED_PAGES = {"homepage", "tc_check_page", "brokers_page", "guide_page", "archive_page", "co_deadlines_page"}

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
  // The /tc-check upload reports its own picked/failed steps through this.
  window.taEvt = evt;
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
  // A hand on the drop box, not just the box on screen (it's above the
  // fold on /tc-check, so dropzone_seen fires on load): click/tap to
  // browse, a file dragged over the page, or the email field focused.
  if(dz){
    var touched = function(){ evt('dropzone_interact'); };
    dz.addEventListener('click', touched);
    document.addEventListener('dragenter', function(e){
      var t = e.dataTransfer && e.dataTransfer.types;
      if(t && Array.prototype.indexOf.call(t, 'Files') !== -1) touched();
    });
    var em = document.getElementById('homeEmailOptinInput') || document.getElementById('emailOptinInput');
    if(em) em.addEventListener('focus', touched);
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
            and page in _TRACKED_PAGES and re.fullmatch(r"[0-9a-f]{32}", visitor)):
        track_event("page_engagement", None, {
            "type": etype, "page": page, "visitor": visitor,
            "device": "mobile" if _is_mobile_request() else "desktop",
        })
    return "", 204


# Pages that carry the nav "Check a file free" (and other check CTAs) but
# never called track_page_view(), so a click there was invisible. Added
# 2026-10-13. Done as one after_request hook keyed on the endpoint instead of
# editing each route: /trust and /tc-hub/colorado build their HTML by
# slicing privacy() and tc_hub() as plain strings, which a Response
# wouldn't survive. Side effect worth knowing on /analytics: from this
# date "Real visitors" also counts people who only saw these pages.
_BEACON_ONLY_PAGES = {
    "pricing": "pricing_page", "tc_hub": "tc_hub_page", "tc_hub_colorado": "co_hub_page",
    "trust_center": "trust_page", "about": "about_page", "faq": "faq_page",
    "terms": "terms_page", "privacy": "privacy_page", "trec_changes": "trec_changes_page", "contact": "contact_page",
    # /demo, 2026-10-14: ads point here, and without a page_view its ?src=
    # never reached the /analytics channel tables.
    "demo": "demo_page",
    # /text-an-offer, 2026-10-24: the homepage section's "Learn more".
    "text_an_offer": "text_offer_page",
}
_TRACKED_PAGES.update(_BEACON_ONLY_PAGES.values())


@app.after_request
def _track_beacon_only_pages(resp):
    page = _BEACON_ONLY_PAGES.get(request.endpoint or "")
    if (page and request.method == "GET" and resp.status_code == 200 and resp.mimetype == "text/html"
            and not resp.direct_passthrough):
        track_page_view(resp, page)
    return resp


# Who may put our pages in an <iframe>. Until 2026-10-13 the site sent no
# framing headers at all, so any site could frame /login or /admin/crm
# (clickjacking). Now: same-origin only (the review page frames its own
# /offers PDF), except the CBS1 calculator widget, which exists to be
# embedded on brokerage and TC sites. Both headers: X-Frame-Options for old
# browsers, frame-ancestors because it overrides XFO where supported.
_EMBEDDABLE_ENDPOINTS = {"co_deadlines_embed"}


@app.after_request
def _frame_headers(resp):
    if request.endpoint in _EMBEDDABLE_ENDPOINTS:
        resp.headers.pop("X-Frame-Options", None)
        resp.headers["Content-Security-Policy"] = "frame-ancestors *"
    else:
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'self'")
    return resp


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


# --- Texas / Colorado site mode -------------------------------------------
# The homepage and /tc-check render BOTH states' copy (data-state="tx"/"co"
# blocks) and a class on <html> hides the inactive one -- so crawlers see
# both and a visitor never sees the wrong state flash in before JS runs.
# Texas is the default. On <html>, not <body>: track_page_view() injects
# data-page by replacing the literal "<body>" tag.
_SITE_STATES = ("tx", "co")


def _is_co_src(src):
    # co1012_..., co_..., li_co_... -- not any word that happens to start
    # with "co" (cold_*, comedy_*, contact_*).
    return bool(re.match(r"^(co\d|co_|li_co)", (src or "").lower()))


def _site_state():
    """Which state a page renders first: ?state= wins, then a Colorado
    campaign src tag on this link (co1012_..., li_co_...), then the
    ta_state cookie the switch sets, then a Colorado first-touch ta_src
    cookie. Anything else is Texas."""
    q = (request.args.get("state") or "").lower()
    if q in _SITE_STATES:
        return q
    if _is_co_src(request.args.get("src")):
        return "co"
    c = (request.cookies.get("ta_state") or "").lower()
    if c in _SITE_STATES:
        return c
    return "co" if _is_co_src(request.cookies.get("ta_src")) else "tx"


def _apply_site_state(html, state):
    pressed = {"tx": "false", "co": "false", state: "true"}
    return (html.replace("__STATE_CLASS__", "state-" + state)
                .replace("__TX_PRESSED__", pressed["tx"])
                .replace("__CO_PRESSED__", pressed["co"]))


def _remember_site_state(resp, state):
    """An explicit ?state= or a Colorado ?src= link is remembered, so the
    visitor's next click (nav, /tc-check) stays in the same state. Toggling
    the switch sets the same cookie from JS."""
    if (request.args.get("state") or "").lower() in _SITE_STATES or _is_co_src(request.args.get("src")):
        resp.set_cookie("ta_state", state, max_age=365 * 24 * 3600, samesite="Lax")


# Same switch markup on both pages; light = the white-card variant.
STATE_SWITCH_JS = """<script>
(function(){
  var root = document.documentElement;
  window.taState = function(){ return root.classList.contains('state-co') ? 'co' : 'tx'; };
  function setState(s){
    if(s !== 'tx' && s !== 'co') return;
    root.classList.remove('state-tx', 'state-co');
    root.classList.add('state-' + s);
    document.querySelectorAll('[data-set-state]').forEach(function(b){
      b.setAttribute('aria-pressed', b.getAttribute('data-set-state') === s ? 'true' : 'false');
    });
    try{ document.cookie = 'ta_state=' + s + '; max-age=31536000; path=/; SameSite=Lax' + (location.protocol === 'https:' ? '; Secure' : ''); }catch(e){}
    try{
      var u = new URL(location.href);
      if(s === 'co') u.searchParams.set('state', 'co'); else u.searchParams.delete('state');
      history.replaceState(history.state, '', u.pathname + u.search + u.hash);
    }catch(e){}
    try{ document.dispatchEvent(new CustomEvent('ta:state', {detail: s})); }catch(e){}
  }
  document.addEventListener('click', function(e){
    var b = e.target.closest && e.target.closest('[data-set-state]');
    if(b) setState(b.getAttribute('data-set-state'));
  });
  // Old links to the removed homepage #colorado section, and /tc-check#colorado.
  if(location.hash === '#colorado' && window.taState() !== 'co') setState('co');
})();
</script>"""


# --- Site chrome: nav + footer (2026-10-23) ---------------------------------
# Shared by the simplified homepage and /tc-check, so the two can't drift.
# The CSS uses each page's own --green/--ink/--mute/--gray/--line variables.
# "html nav.nav" / ".nav .act .nav-cta": NAV_CTA_CSS (injected after the
# page's <style>) restyles every .nav-cta; these win on specificity.
_SITE_CHROME_CSS = """  /* ---------- nav: 4 links + one action ---------- */
  html nav.nav{position:sticky;top:0;z-index:10;height:52px;display:flex;align-items:center;justify-content:space-between;
       padding:0 max(22px,calc((100% - 1024px)/2));background:rgba(255,255,255,.86);
       -webkit-backdrop-filter:saturate(180%) blur(20px);backdrop-filter:saturate(180%) blur(20px);
       border-bottom:1px solid rgba(0,0,0,.06)}
  .nav img{height:19px;width:auto;display:block}
  .nav ul{display:flex;gap:34px;list-style:none;font-size:13px;color:#424245}
  .nav ul .m-only{display:none}
  .nav ul .on{color:var(--ink);font-weight:600}
  .nav .act{display:flex;align-items:center;gap:22px;font-size:13px}
  .nav .act .login{color:#424245}
  .nav .act .nav-cta{background:var(--green);color:#fff;font-weight:600;padding:7px 15px;border-radius:980px;font-size:13px;margin:0;white-space:nowrap}
  .burger{display:none;width:18px;height:12px;background:none;border:none;border-top:1.5px solid #424245;border-bottom:1.5px solid #424245;position:relative;cursor:pointer;padding:0}
  .burger:after{content:"";position:absolute;left:0;right:0;top:4px;border-top:1.5px solid #424245}
  /* ---------- footer ---------- */
  footer{background:var(--gray);border-top:1px solid var(--line);padding:22px max(22px,calc((100% - 1024px)/2)) 30px;font-size:12px;color:var(--mute)}
  footer .legal{line-height:1.6;padding-bottom:16px;border-bottom:1px solid var(--line)}
  .cols{display:grid;grid-template-columns:repeat(4,1fr);gap:20px;padding:18px 0;border-bottom:1px solid var(--line)}
  .cols b{display:block;color:var(--ink);font-weight:600;margin-bottom:8px}
  .cols a{display:block;margin-bottom:6px}
  .copy{padding-top:14px}
  @media (max-width:734px){
    .nav ul,.nav .login{display:none}
    /* The burger opens the same 4 links (plus Log in) as a drop-down. */
    .nav ul.open{display:flex;flex-direction:column;gap:0;position:absolute;top:52px;left:0;right:0;background:#fff;
                 border-bottom:1px solid var(--line);padding:6px 22px 12px;font-size:16px;text-align:left}
    .nav ul.open li a{display:block;padding:11px 0}
    .nav ul.open .m-only{display:block}
    .burger{display:block}
    .cols{grid-template-columns:1fr 1fr}
  }
"""


def _site_nav(text_href="/#text", cta_href="/tc-check", active=""):
    """The nav bar plus its burger script. text_href is "#text" on the
    homepage (same-page anchor); cta_href is "#check" on /tc-check, where
    the drop box is the page itself."""
    return """<nav class="nav">
  <a href="/"><img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer"></a>
  <ul id="navLinks">
    <li><a href="/tc-check"{tc_on} data-evt="nav_check_link">TC Check</a></li>
    <li><a href="{text_href}">Text an offer</a></li>
    <li><a href="/brokers">Brokers</a></li>
    <li><a href="/pricing">Pricing</a></li>
    <li class="m-only"><a href="/login">Log in</a></li>
  </ul>
  <div class="act">
    <a class="login" href="/login">Log in</a>
    <a href="{cta_href}" class="nav-cta" data-evt="nav_check_cta">Check a file</a>
    <button type="button" class="burger" id="navToggle" aria-label="Menu" aria-expanded="false" aria-controls="navLinks"></button>
  </div>
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

""".format(text_href=text_href, cta_href=cta_href,
                 tc_on=' class="on" aria-current="page"' if active == "tc" else "")


_SITE_FOOTER = """<footer>
  <div class="legal">TC Check flags likely issues on a filled form. It is not legal advice and does not replace your own review. TxtAnOffer is not affiliated with the Texas Real Estate Commission (TREC) or the Colorado Real Estate Commission. Built by Phanel Jean-Baptiste, a software builder, not a licensed agent or broker. Examples use sample contracts with planted mistakes, not client files.</div>
  <div class="cols">
    <div><b>Check</b><a href="/tc-check" data-evt="footer_check_link">TC Check</a><a href="/tc-check/bulk">Bulk check</a><a href="/tc-hub">TC Hub</a><a href="/tc-hub/colorado">Colorado TC Hub</a></div>
    <div><b>Learn</b><a href="/guides">Guides</a><a href="/tools/colorado-deadlines" data-evt="states_co_tool">Deadline calculator</a><a href="/faq">FAQ</a><a href="/about">About</a></div>
    <div><b>Brokerages</b><a href="/brokers">For brokers</a><a href="/pricing">Pricing</a><a href="/login">Brokerage sign-in</a></div>
    <div><b>Trust</b><a href="/trust">Trust Center</a><a href="/privacy">Privacy</a><a href="/terms">Terms</a><a href="mailto:support@txtanoffer.com">support@txtanoffer.com</a></div>
  </div>
  <div class="copy">&copy; 2026 TxtAnOffer &middot; Serving Texas &amp; Colorado</div>
</footer>
"""


def _home_trust() -> str:
    """Homepage section 5. The encryption card appears only while
    FILE_ENCRYPTION_KEY is set (same rule as the Trust Center), so the
    homepage never claims encryption that isn't running."""
    cards = [("Files you check", "Deleted",
              "Read for the report, then deleted. We keep short issue codes, not the file.")]
    if filecrypt.enabled():
        cards.append(("Offer PDFs and archived contracts", "Encrypted",
                      "Encrypted with AES&#8209;256 on our servers, and decrypted only when they&rsquo;re opened, downloaded, sent or checked."))
    cards += [("Links to your PDFs", "Private", "Signed and set to expire, so they can&rsquo;t be guessed or shared forever."),
              ("Brokerage archive", "Your team only",
               ("Sign in with Google or an emailed link" if google_auth.enabled() else "Passwordless sign-in")
               + " for the emails on your account. Each brokerage sees only its own files.")]
    grid = "".join(f'<div><span class="ptag">{t}</span><b>{h}</b><p>{p}</p></div>' for h, t, p in cards)
    return f"""<section class="trust">
  <h2>Checked.<br>Then deleted.</h2>
  <p class="sub">A file you check is read for the report, then deleted. We keep short issue codes, not the file (bulk checks also keep file names). Two exceptions: a file you choose to send us for a hand check (it stays in our email inbox), and contracts sent to a Brokerage-plan archive, kept 5 years.</p>
  <div class="protect">{grid}</div>
  <div class="facts facts4">
    <span><i>&#10003;</i>Built on the published TREC and Colorado forms</span>
    <span><i>&#10003;</i>No AI reads your contract</span>
    <span><i>&#10003;</i>Flags issues, not legal advice</span>
    <span><i>&#10003;</i>A real person answers</span>
  </div>
  <div class="ctas"><a class="link" href="/trust#security">See everything that&rsquo;s protected</a></div>
</section>"""


@app.route("/")
def index():
    # Simplified 2026-10-23 (ad-creative mockup, Phanel-approved, copy passed
    # by txo-trec-qa): five sections and a footer, one idea each. What left
    # the page has a home elsewhere: the drop box and the full "what it
    # checks" list are on /tc-check (#what-it-checks), the archive steps and
    # the offer-to-closing workflow on /brokers, the trust detail on /trust.
    # Colorado is the same page with the data-state="co" blocks shown (hero
    # copy, scrap and sample card from the previous homepage); the bulk
    # broker section is Texas-only, like /tc-check/bulk itself.
    html = """
<!DOCTYPE html>
<html lang="en" class="__STATE_CLASS__">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>TxtAnOffer — Catch it before title does.</title>
  <meta name="description" content="For transaction coordinators and managing brokers in Texas and Colorado: drop a filled TREC 20-19 or CBS1 and see what's missing or inconsistent before title kicks it back. TxtAnOffer also drafts new offers by text message.">
  <meta property="og:title" content="TxtAnOffer — Catch it before title does.">
  <meta property="og:description" content="Contract error-checker for Texas & Colorado real estate. Upload a filled TREC resale (20-19), condo (30-18), land (9-18) or farm and ranch (25-17) contract, or a Colorado CBS1, and get a report of what's blank, missing or doesn't add up before title kicks it back.">
  <meta property="og:url" content="https://txtanoffer.com/">
  <meta property="og:type" content="website">
  <meta name="twitter:card" content="summary">
  <meta name="twitter:title" content="TxtAnOffer — Catch it before title does.">
  <meta name="twitter:description" content="Contract error-checker for Texas & Colorado real estate. Upload a filled TREC resale (20-19), condo (30-18), land (9-18) or farm and ranch (25-17) contract, or a Colorado CBS1, and get a report of what's blank, missing or doesn't add up before title kicks it back.">
  <link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
  <style>
  /* One accent: brand green #0b5d52. Red appears only inside product findings. */
  :root{
    --green:#0b5d52; --green-deep:#0a3f3a; --ink:#1d1d1f; --mute:#6e6e73;
    --gray:#f5f5f7; --line:#e3e3e6; --red:#c62828; --amber:#9a6200;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html{-webkit-font-smoothing:antialiased;scroll-behavior:smooth}
  html.state-tx [data-state="co"],html.state-co [data-state="tx"]{display:none !important}
  body{font-family:Inter,-apple-system,system-ui,sans-serif;color:var(--ink);background:#fff;line-height:1.4}
  a{color:inherit;text-decoration:none}

  __SITE_CHROME_CSS__

  /* ---------- shared type scale (3 sizes only) ---------- */
  section{padding:110px 22px 0;text-align:center;overflow:hidden}
  .eyebrow{font-size:19px;font-weight:600;color:var(--green);margin-bottom:10px}
  h1{font-size:80px;line-height:1.04;letter-spacing:-.035em;font-weight:800}
  h2{font-size:56px;line-height:1.07;letter-spacing:-.03em;font-weight:800}
  .sub{font-size:24px;line-height:1.35;color:var(--mute);font-weight:500;max-width:680px;margin:18px auto 0}
  .ctas{display:flex;gap:14px;justify-content:center;flex-wrap:wrap;margin-top:30px}
  .btn{display:inline-block;font-size:17px;font-weight:600;padding:13px 26px;border-radius:980px;border:1.5px solid var(--green)}
  .btn.solid{background:var(--green);color:#fff}
  .btn.ghost{color:var(--green)}
  .link{font-size:19px;color:var(--green);font-weight:600}
  .link:after{content:" \\203A"}
  .fine{font-size:13px;color:var(--mute);margin-top:18px}

  /* ---------- 1. hero: product as the visual ---------- */
  .hero{padding-top:84px}
  .stage{position:relative;max-width:1000px;height:480px;margin:64px auto 0}
  .paper{position:absolute;left:0;top:40px;width:460px;background:#fffdf8;border-radius:6px;padding:22px 26px 30px;
         box-shadow:0 30px 60px -20px rgba(0,0,0,.18);transform:rotate(-3deg);text-align:left;font-family:Georgia,serif;font-size:15px;color:#333}
  .paper .ph{display:flex;justify-content:space-between;font:600 10px Inter,sans-serif;letter-spacing:.08em;color:#888;text-transform:uppercase;margin-bottom:14px}
  .paper .row{display:flex;justify-content:space-between;border-bottom:1px dotted #bbb;padding:7px 0;position:relative}
  .paper .row b{font-family:"Courier New",monospace;color:#1f3c88;font-weight:700}
  .paper .circ svg{position:absolute;right:-14px;top:-8px;width:160px;height:46px}
  .scrawl{font:italic 700 24px "Bradley Hand","Segoe Print",cursive;color:var(--red);text-align:right;margin-top:8px;transform:rotate(-4deg)}
  .card{position:absolute;right:0;top:0;width:520px;background:#fff;border-radius:22px;text-align:left;overflow:hidden;
        box-shadow:0 40px 80px -24px rgba(10,63,58,.35),0 0 0 1px rgba(0,0,0,.04)}
  .card-h{background:var(--green-deep);display:flex;justify-content:space-between;align-items:center;padding:16px 22px}
  .card-h img{height:17px;width:auto}
  .card-h span{font-size:11px;font-weight:700;letter-spacing:.1em;color:#cfe3df}
  .card-b{padding:20px 22px 8px}
  .addr{font-size:21px;font-weight:700;letter-spacing:-.01em}
  .addr-sub{font-size:13px;color:var(--mute);margin-top:2px}
  .banner{background:#fdecec;border-radius:12px;padding:11px 14px;margin:14px 0 4px;font-size:14px;color:var(--red);font-weight:600}
  .banner small{display:block;font-size:12.5px;margin-top:2px;font-weight:600}
  .finding{display:flex;gap:12px;padding:13px 0;border-bottom:1px solid var(--line)}
  .finding:last-child{border-bottom:none}
  .tag{flex:none;font-size:10.5px;font-weight:800;letter-spacing:.04em;padding:4px 8px;border-radius:6px;height:fit-content}
  .tag.b{background:#fdecec;color:var(--red)}
  .tag.w{background:#fff4dc;color:var(--amber)}
  .finding b{display:block;font-size:15px}
  .finding span{font-size:13px;color:var(--mute);line-height:1.4}

  /* ---------- 2. what it catches: 2x2 tiles ---------- */
  .catches{background:var(--gray);padding-bottom:110px;margin-top:110px}
  .tiles{display:grid;grid-template-columns:1fr 1fr;gap:12px;max-width:1024px;margin:56px auto 0}
  .tile{background:#fff;border-radius:28px;padding:56px 30px 50px;min-height:330px;display:flex;flex-direction:column;align-items:center;justify-content:center}
  .tile .big{font-size:50px;font-weight:800;letter-spacing:-.03em;line-height:1.1;position:relative;display:inline-block}
  .tile .mono{font-family:"SF Mono",Menlo,monospace;font-weight:600;font-size:34px;letter-spacing:-.01em}
  .tile p{font-size:19px;color:var(--mute);font-weight:500;margin-top:18px}
  .tile .tag{margin-bottom:22px;font-size:12px;padding:5px 10px}
  .ring{position:absolute;inset:-14px -22px;width:calc(100% + 44px);height:calc(100% + 28px);overflow:visible}
  .blank{display:inline-block;width:150px;border-bottom:3px solid var(--ink);margin-left:8px;position:relative;top:-6px}
  .neq{color:var(--red)}
  .tile .big.eff{font-size:40px}

  /* ---------- 3. text an offer: dark, phone ---------- */
  .text{background:var(--green-deep);color:#fff}
  .forms{list-style:none;display:flex;flex-wrap:wrap;justify-content:center;gap:8px;margin:18px auto 0;padding:0;max-width:640px}
  .forms li{font-size:14px;padding:6px 12px;border-radius:999px;background:#E7F3F1;color:#0a3f3a;white-space:nowrap}
  .forms b{font-weight:700;margin-right:4px}
  /* Texas | Colorado switch: the /tc-check .state-switch look, above the h1.
     The Colorado addenda on the CO fine line are the ones co_audit
     cross-checks against the CBS1 (co_forms). */
  .hero .state-switch{display:inline-flex;background:#ececf0;border-radius:980px;padding:4px;margin:4px 0 22px;white-space:nowrap}
  .hero .state-switch button{border:0;background:none;font:600 15px Inter,sans-serif;color:#424245;padding:9px 20px;border-radius:980px;cursor:pointer}
  .hero .state-switch button small{font-weight:500;color:var(--mute);margin-left:4px}
  .hero .state-switch button[aria-pressed="true"]{background:#fff;color:var(--ink);box-shadow:0 1px 3px rgba(0,0,0,.12)}
  .hero .state-switch button:focus-visible{outline:3px solid #f5c242;outline-offset:2px}
  .text .eyebrow{color:#9fd4c9}
  .text .sub{color:#b9d3ce}
  .text .btn.solid{background:#fff;color:var(--green-deep);border-color:#fff}
  .text .btn.ghost{color:#fff;border-color:rgba(255,255,255,.55)}
  .text .fine{color:#8fb1aa;max-width:560px;margin-left:auto;margin-right:auto}
  .text .fine a{text-decoration:underline}
__TEXT_DEMO_CSS__

  /* ---------- 4. brokers: one oversized number ---------- */
  .brokers{padding-bottom:110px}
  .num{font-size:220px;font-weight:800;letter-spacing:-.06em;line-height:.9;color:var(--green);margin-bottom:10px}

  /* ---------- 5. trust ---------- */
  .trust{background:var(--gray);padding-bottom:110px}
  .facts{display:flex;justify-content:center;gap:56px;margin-top:44px;font-size:17px;font-weight:600;flex-wrap:wrap}
  .facts span{display:flex;align-items:center;gap:8px}
  .facts i{width:22px;height:22px;border-radius:50%;background:var(--green);color:#fff;font-style:normal;font-size:12px;display:grid;place-items:center}
  .protect{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;max-width:980px;margin:44px auto 0;text-align:left}
  .protect div{background:#fff;border:1px solid var(--line);border-radius:18px;padding:24px 24px 26px}
  .protect b{display:block;font-size:19px;font-weight:700;letter-spacing:-.01em}
  .protect .ptag{display:inline-block;margin-bottom:12px;font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--green)}
  .facts4{gap:20px 34px;font-size:16px}
  .protect p{margin-top:8px;font-size:16px;line-height:1.45;color:var(--mute)}

  /* ---------- mobile (734px break) ---------- */
  @media (max-width:734px){
    section{padding-top:72px}
    .hero{padding-top:52px}
    .eyebrow{font-size:17px}
    h1{font-size:46px}
    .hero .state-switch{margin-bottom:16px}
    .hero .state-switch button{padding:8px 14px;font-size:14px}
    h2{font-size:36px}
    .sub{font-size:19px}
    .btn{font-size:16px;padding:12px 22px}
    .stage{height:auto;margin-top:44px}
    .paper{position:relative;width:88%;top:0;font-size:13px;padding:16px 18px 20px}
    .paper .circ svg{width:130px;right:-12px}
    .paper .ph span:last-child{display:none}
    .tile .big.eff{font-size:30px}
    .card{position:relative;width:100%;margin-top:-34px}
    .catches{margin-top:72px;padding-bottom:72px}
    .tiles{grid-template-columns:1fr;margin-top:40px}
    .tile{min-height:250px;padding:44px 20px 40px}
    .tile .big{font-size:40px}
    .tile .mono{font-size:26px}
    .tile p{font-size:17px}
    .blank{width:100px}
    .brokers,.trust{padding-bottom:72px}
    .num{font-size:150px}
    .facts{flex-direction:column;align-items:center;gap:16px}
  }
  </style>
</head>
<body>

__SITE_NAV__
<!-- 1. HERO: one idea, the product doing it -->
<section class="hero">
  <div class="eyebrow">TC Check &middot; contract error-checker for Texas &amp; Colorado real estate</div>
  <!-- Texas | Colorado switch (2026-10-26): same buttons and STATE_SWITCH_JS
       as /tc-check, so Colorado isn't hidden behind a fine-print link. -->
  <div class="state-switch" role="group" aria-label="Which state's contracts">
    <button type="button" data-set-state="tx" aria-pressed="__TX_PRESSED__" data-evt="state_toggle_tx">Texas <small>TREC</small></button>
    <button type="button" data-set-state="co" aria-pressed="__CO_PRESSED__" data-evt="state_toggle_co">Colorado <small>CBS1 &amp; Land</small></button>
  </div>
  <h1>Catch it before<br>title does.</h1>
  <p class="sub" data-state="tx">One checker for the Texas contracts you file most: resale, condo, land, and farm and ranch. Upload a filled TREC contract and in seconds you get a report flagging the blank fields, bad math and impossible dates title would kick back. Built for TCs, agents and brokerages.</p>
  <ul class="forms" data-state="tx" aria-label="Contracts TC Check reads">
    <li><b>20-19</b> Resale</li><li><b>30-18</b> Condo</li><li><b>9-18</b> Land</li><li><b>25-17</b> Farm &amp; Ranch</li>
  </ul>
  <p class="sub" data-state="co">Drop a filled CBS1 or Colorado Land contract and see what title would kick back in seconds &mdash; &sect;3.1 deadlines left blank that delete a contingency, &sect;4.1 price math that doesn&rsquo;t add up, deadlines that land after closing. Built for TCs, agents and brokerages.</p>
  <ul class="forms" data-state="co" aria-label="Colorado contracts TC Check reads">
    <li><b>CBS1</b> Residential</li><li><b>CO Land</b> Land &amp; acreage</li>
  </ul>
  <div class="ctas" data-state="tx">
    <a class="btn solid" href="/tc-check" data-evt="hero_cta">Check a file free</a>
    <a class="btn ghost" href="/tc-check?demo=1" data-evt="hero_sample">See a sample report</a>
  </div>
  <div class="ctas" data-state="co">
    <a class="btn solid" href="/tc-check?state=co" data-evt="hero_cta">Check a file free</a>
    <a class="btn ghost" href="/tc-check?demo=1&amp;state=co" data-evt="hero_sample">See a sample report</a>
  </div>
  <p class="fine" data-state="tx">3 free reports, no signup &middot; 4 Texas TREC contracts + the 40-11, 41-3 and 39-11</p>
  <p class="fine" data-state="co">3 free reports, no signup &middot; Colorado CBS1 &amp; Land + Amend/Extend and Counterproposal</p>

  <!-- Illustration only. Scrap values are the sample files' own fields;
       card counts and findings are the sample report's display_issues,
       with the full message text from first_screen.SAMPLE_ROWS
       (tests/test_site_polish.py and test_first_screen.py check both). -->
  <div class="stage" aria-hidden="true">
    <div class="paper" data-state="tx">
      <div class="ph"><span>Contract concerning 123 Main St</span><span>Page 1 of 12</span></div>
      <p style="font-weight:700;margin-bottom:6px">3. SALES PRICE:</p>
      <div class="row"><span>A. Cash portion</span><b>$85,000</b></div>
      <div class="row"><span>B. Sum of all financing</span><b>$340,000</b></div>
      <div class="row circ"><span>C. Sales Price</span><b>$435,000</b>
        <svg viewBox="0 0 160 46" preserveAspectRatio="none"><path d="M40 8 C 90 -2, 156 4, 156 22 C 156 40, 90 46, 50 42 C 14 38, 6 22, 30 12" fill="none" stroke="#c62828" stroke-width="3" stroke-linecap="round"/></svg></div>
      <div class="scrawl">= $425,000?</div>
    </div>
    <div class="paper" data-state="co">
      <div class="ph"><span>CBS1 &middot; 123 Main St, Denver</span><span>&sect;4.1</span></div>
      <p style="font-weight:700;margin-bottom:6px">4.1 Price and Terms</p>
      <div class="row"><span>Earnest Money</span><b>$10,000</b></div>
      <div class="row"><span>New Loan</span><b>$400,000</b></div>
      <div class="row"><span>Cash at Closing</span><b>$80,000</b></div>
      <div class="row circ"><span>Purchase Price</span><b>$500,000</b>
        <svg viewBox="0 0 160 46" preserveAspectRatio="none"><path d="M40 8 C 90 -2, 156 4, 156 22 C 156 40, 90 46, 50 42 C 14 38, 6 22, 30 12" fill="none" stroke="#c62828" stroke-width="3" stroke-linecap="round"/></svg></div>
      <div class="scrawl">= $490,000?</div>
    </div>
    <div class="card" id="heroReportCard">
      <div class="card-h"><img src="/static/logo-wordmark-white.png?v=1" alt=""><span>TC CHECK REPORT</span></div>
      <div class="card-b" data-state="tx">
        <div class="addr">123 Main St, Austin, TX 78704</div>
        <div class="addr-sub">Sample TREC 20-19 with planted mistakes</div>
        <div class="banner">Not ready &mdash; fix before this goes to title<small>12 must fix &middot; 4 to review</small></div>
        __HOME_FINDINGS_TX__
      </div>
      <div class="card-b" data-state="co">
        <div class="addr">123 Main St, Denver, CO</div>
        <div class="addr-sub">Sample CBS1 with planted mistakes</div>
        <div class="banner">Not ready &mdash; fix before this goes to title<small>1 must fix &middot; 2 to review</small></div>
        __HOME_FINDINGS_CO__
      </div>
    </div><!-- /heroReportCard -->
  </div>
</section>

<!-- 2. WHAT IT CATCHES: one finding per tile, huge type -->
<section class="catches">
  <h2>What it flags<br>before title sees it.</h2>
  <p class="sub">Four of the things TC Check flags.</p>
  <div class="tiles">
    <div class="tile">
      <span class="tag b">MUST FIX</span>
      <div class="big" data-state="tx">3A + 3B <span class="neq">&ne;</span> 3C</div>
      <div class="big" data-state="co">&sect;4.1 <span class="neq">&ne;</span> Price</div>
      <p data-state="tx">A sales price that doesn&rsquo;t add up.</p>
      <p data-state="co">A &sect;4.1 price table that doesn&rsquo;t add up to the Purchase Price.</p>
    </div>
    <div class="tile" data-state="tx">
      <span class="tag b">MUST FIX</span>
      <div class="big">November 31
        <svg class="ring" viewBox="0 0 300 80" preserveAspectRatio="none" aria-hidden="true"><path d="M60 6 C 180 -4, 296 6, 294 40 C 292 74, 160 80, 80 74 C 10 68, 0 30, 40 14" fill="none" stroke="#c62828" stroke-width="3" stroke-linecap="round"/></svg>
      </div>
      <p>A closing date that doesn&rsquo;t exist.</p>
    </div>
    <div class="tile" data-state="tx">
      <span class="tag b">MUST FIX</span>
      <div class="big eff">Effective Date<span class="blank"></span></div>
      <p>The date the contract&rsquo;s deadlines count from, left blank.</p>
    </div>
    <div class="tile" data-state="tx">
      <span class="tag w">REVIEW</span>
      <div class="mono">john.doe@gmail<span class="neq">com</span></div>
      <p>A notice email that would bounce.</p>
    </div>
    <div class="tile" data-state="co">
      <span class="tag w">REVIEW</span>
      <div class="big">&sect;3.1 N/A</div>
      <p>A loan deadline marked N/A while &sect;4.1 still has a New Loan.</p>
    </div>
    <div class="tile" data-state="co">
      <span class="tag w">REVIEW</span>
      <div class="big">11/11 closing</div>
      <p>A Closing Date that lands on a holiday, when banks may not fund.</p>
    </div>
    <div class="tile" data-state="co">
      <span class="tag w">REVIEW</span>
      <div class="big eff">Contract Date<span class="blank"></span></div>
      <p>The contract Date on page 1 left blank.</p>
    </div>
  </div>
  <div class="ctas"><a class="link" href="/tc-check#what-it-checks" data-evt="home_check_link">See everything it checks</a></div>
</section>

<!-- 3. TEXT AN OFFER: the namesake product, played as a 3-step demo (text_demo.py) -->
<section class="text" id="text">
  <div class="eyebrow">Also from TxtAnOffer: Text an offer</div>
  <h2>Text the terms.<br>Get a filled draft back.</h2>
  <p class="sub" data-state="tx">Price, down payment, closing days, address. Reply YES and the TREC 20&#8209;19 comes back with your terms filled in, ready to review.</p>
  <p class="sub" data-state="co">Price, down payment, closing days and a Colorado address. Reply YES and the CBS1 comes back with your terms filled in, ready to review.</p>
  <div class="ctas">
    <a class="btn solid" href="sms:+18338970333" data-evt="workflow_offer_sms">Text (833) 897-0333</a>
    <a class="btn ghost" href="/pricing">See pricing</a>
    <a class="btn ghost" href="/text-an-offer" data-evt="text_learn_more">Learn more</a>
  </div>
  <p class="fine">By texting, you agree to receive SMS replies and reminders about your offers, plus up to 2 tips on the free trial. Reply STOP to opt out, HELP for help. Msg frequency varies. Msg &amp; data rates may apply. <a href="/privacy">Privacy</a> &middot; <a href="/terms">Terms</a></p>
__TEXT_DEMO__
</section>

<!-- 4. BROKERS: one number. Texas only: /tc-check/bulk checks TREC 20-19s. -->
<section class="brokers" data-state="tx">
  <div class="num">20</div>
  <h2>Closed files.<br>Checked free.</h2>
  <p class="sub">Managing broker? Zip up to 20 of your agents&rsquo; closed contracts and get one free report by email.</p>
  <div class="ctas">
    <a class="btn solid" href="/tc-check/bulk" data-evt="hero_broker_cta">Check 20 files free</a>
    <a class="btn ghost" href="/brokers" data-evt="path_brokerage_cta">For brokerages</a>
  </div>
</section>

<!-- 5. TRUST: one promise, then what's protected (see _home_trust) -->
__HOME_TRUST__

__SITE_FOOTER__
__TEXT_DEMO_JS__
__STATE_SWITCH_JS__</body>
</html>
"""
    html = (html.replace("__SITE_CHROME_CSS__", _SITE_CHROME_CSS)
                .replace("__SITE_NAV__", _site_nav(text_href="#text"))
                .replace("__SITE_FOOTER__", _SITE_FOOTER)
                .replace("__HOME_TRUST__", _home_trust())
                .replace("__TEXT_DEMO_CSS__", text_demo.CSS)
                .replace("__TEXT_DEMO__", text_demo.HTML)
                .replace("__TEXT_DEMO_JS__", text_demo.JS)
                .replace("__HOME_FINDINGS_TX__", _home_findings("tx"))
                .replace("__HOME_FINDINGS_CO__", _home_findings("co"))
                .replace("__STATE_SWITCH_JS__", STATE_SWITCH_JS))
    # Carry ?src=name (e.g. from a Direct Reach email) onto every /tc-check
    # link on the page, so a click through to TC Check still attributes
    # correctly even though it lands on the homepage first -- see
    # get_landing_visits_by_source() on /analytics. /tc-check also reads
    # its own ?src= for the title-company and November 31 first screens
    # (first_screen.source_variant), which the ta_src cookie can't do.
    import re as _re
    src = _re.sub(r"[^a-zA-Z0-9_-]", "", request.args.get("src", ""))[:60]
    if src:
        def _carry(m):
            query, frag = m.group(1) or "", m.group(2) or ""
            query = "?src=" + src + ("&amp;" + query[1:] if query else "")
            return f'href="/tc-check{query}{frag}"'
        html = _re.sub(r'href="/tc-check(\?[^"#]*)?(#[^"]*)?"', _carry, html)
    site_state = _site_state()
    html = _apply_site_state(html, site_state)

    resp = make_response(html)
    _remember_site_state(resp, site_state)
    # First-touch attribution cookie: the query-param rewrite above only
    # survives if the visitor clicks through in this exact page load.
    # Cold-outreach signups routinely happen on a later visit (a
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


# Bold one-line titles for the homepage sample card; the line under each is
# the finding's full message from first_screen.SAMPLE_ROWS (the same text
# /tc-check's sample strip shows, kept honest by tests/test_first_screen.py).
_HOME_FINDING_TITLES = {
    ("tx", "price"): "Price doesn&rsquo;t add up",
    ("tx", "november31"): "Impossible closing date",
    ("tx", "email"): "Notices will bounce",
    ("co", "price"): "Price doesn&rsquo;t add up",
    ("co", "loan"): "Loan contingency deleted",
    ("co", "holiday"): "Holiday closing",
}


def _home_findings(state):
    return "".join(
        f'<div class="finding"><span class="tag {cls}">{tag}</span><div><b>{_HOME_FINDING_TITLES[(state, key)]}</b>'
        f'<span>{msg}</span></div></div>'
        for key, cls, tag, msg in first_screen.SAMPLE_ROWS[state]
    )


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
        # Colorado drafts as a CBS1 (co_filler.py) instead of being blocked.
        if other_state_display in ("CO", "Colorado"):
            result["state"] = "CO"
            if len(cleaned.split()) < 3:
                result["warnings"].append("Address looks short. Please confirm city is included before signing.")
            result["valid"] = True
            return result
        result["other_state"] = other_state_display
        result["reason"] = (
            f"This address looks like it's in {other_state_display}. "
            f"TxtAnOffer only generates Texas TREC and Colorado CBS1 contracts -- we can't produce a "
            f"legally valid contract for a property in another state."
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
        f"This looks like it's in {other_state}. TxtAnOffer "
        f"currently supports Texas and Colorado. Want to know when we launch in "
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
    if addr_check.get("state") == "CO":
        return _build_co_draft(parsed, source_id), None, warnings
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


def _build_co_draft(parsed: dict, source_id: str) -> dict:
    """Colorado CBS1 draft. Skips lookup_mls() (it forces ", TX" onto the
    search) and the TX state checks; county/city/zip come from the text via
    co_filler.split_co_address. Colorado has no option fee."""
    from co_filler import split_co_address, contract_dates
    parsed["state"] = "CO"
    parsed["_tx_needs_confirm"] = False
    addr = split_co_address(parsed["address"])
    parsed["street"] = addr["street"]
    parsed["city"] = addr["city"]
    parsed["zip"] = addr["zip"]
    parsed["county"] = addr["county"]
    agent = get_agent_profile(source_id)
    parsed["agent"] = agent
    price = parsed["price"]
    parsed["down_payment_amount"] = int(price * parsed["down_payment_pct"])
    parsed["loan_amount"] = price - parsed["down_payment_amount"]
    parsed["earnest_money"] = int(price * agent["default_earnest_pct"])
    parsed["option_fee"] = 0
    # Denver's date, not the host's: Railway runs on UTC, so a 9 PM Mountain
    # text drafted as tomorrow. co_filler defaults to the same colorado_date().
    today = colorado_date()
    acceptance, closing = contract_dates(parsed["close_days"], today)
    parsed["co_acceptance"] = f"{acceptance:%b} {acceptance.day}"
    parsed["co_closing"] = f"{closing:%b} {closing.day}"
    # contract_dates rolls the closing off weekends/holidays too; say so.
    from co_audit import is_business_day as co_business_day
    parsed["co_closing_note"] = roll_note(today + timedelta(days=int(parsed["close_days"])), co_business_day)
    return parsed


def format_co_confirmation(parsed: dict) -> str:
    """Colorado version of format_offer_confirmation. Plain GSM-7 only."""
    where = ", ".join(x for x in (parsed.get("street") or parsed["address"], parsed.get("city"),
                                  ("CO " + parsed["zip"]) if parsed.get("zip") else "CO") if x)
    county = f"{parsed['county']} County" if parsed.get("county") else 'County missing - add it, ex: "Denver county"'
    if parsed["loan_amount"] > 0:
        fin = FINANCING_LABELS.get(parsed.get("financing_type") or "conventional", "Conventional")
        money = [f"Price ${parsed['price']:,}, {_fmt_pct(parsed['down_payment_pct'])}% down",
                 f"{fin} loan ${parsed['loan_amount']:,}"]
    else:
        money = [f"Price ${parsed['price']:,}, all cash"]
    insp = parsed.get("inspection_days") or 10
    lines = ["TxtAnOffer: got it (Colorado).", "", where, county] + money + [
        f"Earnest ${parsed['earnest_money']:,}",
        f"Closing {parsed['co_closing']} ({parsed['close_days']} days"
        + (f", {parsed['co_closing_note']})" if parsed.get("co_closing_note") else ")"),
        f"Offer expires {parsed['co_acceptance']} 5:00 PM",
        "",
        f"Deadlines: standard defaults (inspection objection {insp} days, title, "
        + ("loan, appraisal" if parsed["loan_amount"] > 0 else "no loan") + "). Review each before signing.",
        "",
        'Reply YES to create the Colorado CBS1 draft, or text a correction.',
        "",
        "If you're this buyer's agent or transaction-broker, Colorado law (HB26-1426) requires a signed "
        "written buyer agreement before you perform brokerage services for them, which generally includes "
        "writing the offer. No agreement yet? Text: BUYER AGREEMENT <buyer> <fee % or $> <days>",
        "",
        "Msg rates may apply. Reply STOP to unsubscribe, HELP for help.",
    ]
    return "\n".join(lines)


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


FINANCING_LABELS = {"conventional": "Conventional", "fha": "FHA", "va": "VA", "usda": "USDA",
                    "texas_veterans": "Texas Veterans", "reverse_mortgage": "Reverse Mortgage", "cash": "Cash"}


def _fmt_pct(pct: float) -> str:
    pct100 = pct * 100
    return f"{pct100:.0f}" if pct100 == int(pct100) else f"{pct100:.1f}"


def closing_line(close_days, drafted_at=None) -> str:
    """The SMS summary's closing line, e.g. "Closing Mon, Nov 16, 2026 (21
    days, moved off the weekend)". The date is rolled to the next business
    day (deadlines.offer_closing_date), so a bare "(21 days)" next to a date
    23 days out would be wrong; the note says why it moved."""
    d = offer_closing_date(close_days, drafted_at)
    note = closing_roll_note(close_days, drafted_at)
    days = f"{close_days} days, {note}" if note else f"{close_days} days"
    return f"Closing {d:%a}, {d:%b} {d.day}, {d.year} ({days})"


def format_offer_confirmation(parsed: dict) -> str:
    """The AI Offer Builder's structured confirmation, shown before a PDF is
    generated. Financing/inspection lines only appear when the agent actually
    specified them -- never silently guessed. Agent name/license line is
    omitted if the agent hasn't set up their profile yet."""
    if parsed.get("state") == "CO":
        return format_co_confirmation(parsed)
    addr_parts = [parsed["address"]]
    if parsed.get("city"):
        addr_parts.append(parsed["city"])
    if parsed.get("zip"):
        addr_parts.append(f"TX {parsed['zip']}")
    elif parsed.get("city"):
        addr_parts.append("TX")
    full_addr = ", ".join(addr_parts)

    # Plain GSM-7 characters only (no "|", "\u00b7" or em dashes): one
    # non-GSM character flips the whole SMS to UCS-2, which cuts each segment
    # from 160 to 70 characters and roughly triples what every reply costs.
    if parsed["loan_amount"] > 0:
        money_line = (f"Price ${parsed['price']:,}, {_fmt_pct(parsed['down_payment_pct'])}% down "
                      f"(${parsed['down_payment_amount']:,})")
        loan_line = f"Loan ${parsed['loan_amount']:,}"
    else:
        money_line = f"Price ${parsed['price']:,}, all cash"
        loan_line = None
    lines = [
        "TxtAnOffer: got it.",
        "",
        full_addr,
    ]
    # 2A "County of" prints whatever is here, so the agent sees it before YES
    # (a street or city name used to land in it unseen, e.g. "Denton Dr").
    if parsed.get("county"):
        lines.append(f"County: {parsed['county']}")
    lines.append(money_line)
    if loan_line:
        lines.append(loan_line)
    lines += [
        f"Earnest ${parsed['earnest_money']:,}, option fee ${parsed['option_fee']:,}",
        closing_line(parsed["close_days"]),
    ]
    if parsed.get("financing_type") and parsed["financing_type"] != "cash":
        lines.append(f"Financing: {FINANCING_LABELS.get(parsed['financing_type'], parsed['financing_type'].title())}")
    if parsed.get("inspection_days") is not None:
        lines.append(f"Option period: {parsed['inspection_days']} days")

    lines += [
        "",
        'Reply YES to create the TREC draft, or text a correction (ex: "make it 820k").',
    ]

    agent = parsed.get("agent") or {}
    if agent.get("name"):
        agent_line = agent["name"]
        if agent.get("license"):
            agent_line += f", TREC #{agent['license']}"
        lines += ["", agent_line]

    lines += ["", "Msg rates may apply. Reply STOP to unsubscribe, HELP for help."]
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


def _day_one_lines(filename, address, effective_date, option_days, close_date):
    """Texas Paragraph 5/option summary, or for a Colorado CBS1 the §3.1
    dates printed on that contract (Colorado has no option period)."""
    if transaction_tasks.is_co(filename):
        import co_audit
        from deadlines import build_co_day_one_summary
        values = co_audit.read_contract(os.path.join(OUTPUT_DIR, filename)) or {}
        return build_co_day_one_summary(address, effective_date, values)
    return build_day_one_summary(address, effective_date, option_days, close_date)


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
            status_line = f"\n\n{remaining} free offer{'s' if remaining != 1 else ''} left."
        else:
            payment_url = request.host_url.rstrip("/") + "/pricing"
            status_line = f"\n\nThat was your last free offer. Unlimited offers: {payment_url}"

    if draft.get("state") == "CO":
        twilio_send_sms(agent_phone, (
            f"Your Colorado CBS1 draft for {draft['address']} is ready.\n\n"
            f"Review and email it: {pdf_url}\n\n"
            f"Deadlines are standard defaults, and Earnest Money Holder, title selection and lead-paint "
            f"rows are yours to fill. Draft only: review every page before signing."
            f"{status_line}\n\n"
            f"Reply STOP to unsubscribe, HELP for help."))
        return

    forms = ["TREC 20-19"]
    if draft.get("loan_amount", 0) > 0:
        forms.append("40-11 Financing Addendum")
    if draft.get("has_hoa"):
        forms.append("36-11 HOA Addendum")
    forms += ["61-0 Water Disclosure", "IABS"]
    includes = ", ".join(forms[:-1]) + " and " + forms[-1]

    # One-time nudge on the very first offer only -- an agent with no saved
    # profile sees a much longer blocking-fields checklist on the review
    # page (Title Company, Escrow Agent, Buyer's-agent contact all show up
    # missing) than one who's filled it in once. Told here, once, instead of
    # leaving them to discover the checklist shrinks only by trial and error.
    profile_nudge = ""
    if new_count == 1:
        agent_profile = draft.get("agent") or {}
        if not agent_profile.get("title_company") or not agent_profile.get("business_address"):
            # Signed, same as the PROFILE keyword's link -- a bare /profile
            # is a 403 (the page only opens behind a signed link), so this
            # tip used to dead-end on the one step that clears the blocks.
            profile_url = sign_dashboard_url(agent_phone, request.host_url.rstrip("/")).replace("/dashboard?", "/profile?")
            profile_nudge = (
                f"\n\nTip: add your Title Company & Business Address once at "
                f"{profile_url} and future offers will need far less filled in by hand."
            )

    reply = (
        f"Your TREC draft for {draft['address']} is ready.\n\n"
        f"Review and email it: {pdf_url}\n\n"
        f"Includes {includes}. Draft only: review every page before signing.\n\n"
        f"To change terms, just text the new offer. Reply DASHBOARD for all your offers."
        f"{status_line}"
        f"{profile_nudge}\n\n"
        f"Reply STOP to unsubscribe, HELP for help."
    )
    twilio_send_sms(agent_phone, reply)


SMS_HELP_TEXT = (
    "TxtAnOffer Commands:\n\n"
    "Text your offer terms anytime -- TxtAnOffer turns them into a "
    "TREC 20-19 draft in seconds.\n\n"
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
    "Support: support@txtanoffer.com. Msg freq varies. Msg & data rates may apply. Reply STOP to unsubscribe."
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
    # Recorded for nurture.py, which must never text someone who opted out.
    # Twilio itself answers STOP/START (and blocks our sends after a STOP),
    # so don't also run the word through the offer parser; a free-text
    # "stop texting me" gets no reply either. CANCEL is a Twilio opt-out
    # word too but still cancels a pending draft below.
    nurture.record_opt_keyword(agent_phone, incoming_msg, _form.get("OptOutType", ""))
    _word = incoming_msg.strip().upper()
    if (_word != "CANCEL" and nurture.is_opt_out_text(incoming_msg)) or _word in nurture.OPT_IN_WORDS:
        return "", 200
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

    if keyword.startswith("BUYER AGREEMENT"):
        # Colorado HB26-1426: signed written buyer agreement (CREC BC60
        # Exclusive Right-to-Buy) before writing an offer.
        from co_filler import parse_buyer_agreement_sms, fill_buyer_agreement
        ba = parse_buyer_agreement_sms(incoming_msg)
        if "error" in ba:
            twilio_send_sms(agent_phone, ba["error"] + "\n\nReply STOP to unsubscribe, HELP for help.")
            return "", 200
        from co_filler import EditionUnavailable
        try:
            pdf_path = fill_buyer_agreement(ba, get_agent_profile(agent_phone))
        except EditionUnavailable as e:
            twilio_send_sms(agent_phone, f"{e}\n\nReply STOP to unsubscribe, HELP for help.")
            return "", 200
        except Exception as e:
            print(f"[SMS] BC60 ERROR: {e}")
            twilio_send_sms(agent_phone, "Couldn't create the buyer agreement. Please try again or contact support.")
            return "", 200
        filename = os.path.basename(pdf_path)
        track_event("co_buyer_agreement_generated", agent_phone, {"filename": filename})
        pdf_url = sign_pdf_url(filename, request.host_url.rstrip("/"))
        fee = f"{ba['pct']:g}%" if ba.get("pct") else f"${ba['amount']:,.0f}"
        rel = {"agency": "Buyer Agency", "tb": "Transaction-Brokerage"}.get(ba.get("relationship"))
        prof = get_agent_profile(agent_phone) or {}
        filled = ["buyer"]
        todo = []
        if prof.get("name"):
            filled.append("your name")
        else:
            todo.append("Broker (3.3)")
        if prof.get("brokerage"):
            filled.append("your brokerage")
        else:
            todo.append("Brokerage Firm (3.2)")
        filled += [f"a {ba['days']}-day Listing Period", f"Success Fee {fee}"] + ([rel] if rel else [])
        if not rel:
            todo.append("Buyer Agency or Transaction-Brokerage")
        todo += ["firm type (2.1/2.2)", "property (3.4)", "who pays (7.3)", "holdover (7.4)", "the 9 and 15 boxes"]
        twilio_send_sms(agent_phone,
            f"Colorado Exclusive Right-to-Buy for {ba['buyer']} is ready: {pdf_url}\n\n"
            f"Filled: {', '.join(filled)}.\n"
            f"You fill in: {', '.join(todo)}, then signatures -- review every blank. If 7.3 is left blank, 7.3.1 "
            f"applies: the buyer owes any unpaid part of the fee only if you disclose that amount in writing "
            f"before they sign a contract.\n\n"
            f"Draft only -- review with your buyer before signing.\n\nReply STOP to unsubscribe, HELP for help.")
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
        if (offer.get("filename") or "").startswith("CBS1_"):
            # Colorado: an Agreement to Amend/Extend (AE41), never a TREC 39-11.
            from co_filler import fill_ae41, EditionUnavailable
            try:
                pdf_path = fill_ae41(offer, amend)
            except EditionUnavailable as e:
                twilio_send_sms(agent_phone, str(e))
                return "", 200
            except Exception as e:
                print(f"[SMS] AE41 ERROR: {e}")
                twilio_send_sms(agent_phone, "Couldn't create the Colorado Amend/Extend for that offer. Please try again or contact support.")
                return "", 200
            filename = os.path.basename(pdf_path)
            record_amendment(offer["id"], agent_phone, amend["field"], amend["value"], filename)
            pdf_url = sign_pdf_url(filename, request.host_url.rstrip("/"))
            if amend["field"] == "price":
                change_line = (f"New Purchase Price: ${amend['value']:,}. Update the New Loan and "
                               f"Cash at Closing amounts with the parties before signing.")
            else:
                change_line = "Closing Date extended (moved to a business day if needed)."
            twilio_send_sms(agent_phone,
                f"Agreement to Amend/Extend (Colorado) for {offer['address']}:\n{change_line}\n\n{pdf_url}\n\n"
                f"Draft only -- review before signing.\n\n"
                f"Reply STOP to unsubscribe, HELP for help.")
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
            change_line = amended_closing_line(offer, amend["value"])
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
<meta name="description" content="Text or type the offer terms and get a drafted TREC 20-19 (or Colorado CBS1) to review. You complete the rest before signing.">
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
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
    --text-dim: #5f707f;
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
    background:var(--accent);color:#fff;border:none;
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
    background:var(--accent);color:#fff;
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
    <a href="/tc-check" class="nav-cta" data-evt="nav_check_cta">Check a file free</a>
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
    <h1>Text the terms,<br><span class="gradient">get a draft to review.</span></h1>
    <p class="page-sub">TxtAnOffer drafts a TREC 20-19 (or Colorado CBS1) with the terms you send. You review it and complete the rest before signing.</p>

    <div class="workflow">
      <div class="wf-step"><div class="wf-icon">&#9993;</div><div class="wf-title">You type</div><div class="wf-desc">725k 3% 21day<br>123 Main St, Austin, TX</div></div>
      <div class="wf-arrow">&rarr;</div>
      <div class="wf-step"><div class="wf-icon">&#9881;</div><div class="wf-title">We parse</div><div class="wf-desc">Price, terms &amp;<br>address extracted</div></div>
      <div class="wf-arrow">&rarr;</div>
      <div class="wf-step"><div class="wf-icon">&#9998;</div><div class="wf-title">Draft to review</div><div class="wf-desc">TREC 20-19 or CBS1<br>with your terms filled in</div></div>
    </div>

    <div class="card">
      <form method="POST" action="/demo">
        <label class="field-label">Offer details</label>
        <input type="text" name="offer_text" placeholder="725k 3% 21day 123 Main St, Austin, TX" value="{prefill}">
        <button type="submit">Draft My Contract</button>
        <div class="hint">price &middot; down % &middot; closing days &middot; county (optional) &middot; address &middot; financing type &amp; inspection days (optional)</div>
        <div class="hint">You'll get a confirmation to review first &mdash; reply <code>YES</code> to generate the PDF, <code>NO</code> to cancel, or send corrections.</div>
        <div class="hint">Already sent one? Amend it: <code>AMEND 123 Main St price 730k</code> or <code>AMEND 123 Main St close +10</code></div>
      </form>
      {result_html}
    </div>

    <div class="cmd-menu" id="commands">
      <div class="cmd-menu-title">Text these to 1-833-897-0333</div>
      <div class="cmd-row"><span class="cmd-key">price down% days address</span><span class="cmd-desc">Get a confirmation to review &mdash; e.g. 725k 3% 21day 123 Main St, Austin, TX</span></div>
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
      <div class="trust-item"><div class="trust-val">Draft</div><div class="trust-label">You review before signing</div></div>
      <div class="trust-item"><div class="trust-val">TREC</div><div class="trust-label">Official 20-19 form</div></div>
      <div class="trust-item"><div class="trust-val">HTTPS</div><div class="trust-label">Encrypted in transit</div></div>
    </div>

    <div class="foot">
      By texting or using this service, you consent to receive SMS replies, offer reminders and up to 2 free-trial tips. Reply STOP to opt out anytime. Msg frequency varies. Msg &amp; data rates may apply.
      <br><a href="/pricing">View Pricing</a> &middot; <a href="/terms">Terms</a> &middot; <a href="/privacy">Privacy</a>
    </div>
  </div>
</body>
</html>
"""


# Per-browser key for /demo's pending draft and offer history. Until
# 2026-10-14 every /demo visitor shared one pseudo-id for both, so two
# people on the page at the same time shared one pending draft: B's YES
# generated, and showed B, A's address and price, and "AMEND <address>"
# could pull up another visitor's offer. Now each browser gets a random
# token in an HttpOnly cookie and the key is "demo-web:<token>". The agent
# profile (DEMO_PROFILE) and the PDF filler still get the plain pseudo-id,
# so only the isolation changed. Drafts still expire after
# drafts.DRAFT_TTL_MINUTES (30). The PDFs themselves were never guessable:
# random filename suffix + HMAC-signed /offers and /review links.
# reminders.py skips these offers via its phone NOT LIKE '%demo%' filter.
_DEMO_COOKIE = "ta_demo"


def _demo_visitor_key():
    """Returns (key, token, is_new). A missing or malformed cookie gets a
    fresh token, so a forged value can't name someone else's key unless it
    is their exact 128-bit token."""
    token = request.cookies.get(_DEMO_COOKIE, "")
    is_new = not re.fullmatch(r"[0-9a-f]{32}", token)
    if is_new:
        token = uuid.uuid4().hex
    return f"demo-web:{token}", token, is_new


@app.route("/demo", methods=["GET", "POST"])
def demo():
    demo_key, token, is_new = _demo_visitor_key()
    resp = make_response(_demo_page(demo_key))
    if is_new:
        # 24h = the PDF link lifetime, so AMEND works for as long as the
        # offer's own links do. SameSite=Lax keeps another site from
        # POSTing "YES" to /demo with this visitor's cookie attached.
        resp.set_cookie(_DEMO_COOKIE, token, max_age=PDF_LINK_TTL, httponly=True,
                        samesite="Lax", secure=request.is_secure)
    return resp


def _demo_page(demo_key):
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
                offer = find_recent_offer(demo_key, amend["address"])
                if not offer:
                    result_html = f'<div class="error">No demo offer found matching "{amend["address"]}". Generate an offer for that address above first, then amend it.</div>'
                else:
                    try:
                        if (offer.get("filename") or "").startswith("CBS1_"):
                            from co_filler import fill_ae41
                            pdf_path = fill_ae41(offer, amend)
                        else:
                            pdf_path = fill_amendment_pdf(offer, amend)
                    except Exception as e:
                        result_html = f'<div class="error">Couldn\'t generate amendment: {e}</div>'
                    else:
                        filename = os.path.basename(pdf_path)
                        record_amendment(offer["id"], demo_key, amend["field"], amend["value"], filename)
                        pdf_url = sign_pdf_url(filename)
                        _pdf_expires = int(time.time()) + PDF_LINK_TTL
                        _pdf_sig = hmac.new(PDF_LINK_SECRET.encode(), f"{filename}:{_pdf_expires}".encode(), hashlib.sha256).hexdigest()[:16]
                        change_line = f"New Sales Price: ${amend['value']:,}" if amend["field"] == "price" else amended_closing_line(offer, amend["value"])
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
            draft = get_draft(demo_key)
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
            clear_draft(demo_key)
            parsed, error, warnings = draft, None, []
        elif offer_text.strip().upper() in ("NO", "CANCEL"):
            if get_draft(demo_key):
                clear_draft(demo_key)
                result_html = '<div class="result"><div class="result-stamp">Cancelled</div><p style="color:var(--text-dim);">Offer cancelled. Enter new details above anytime.</p></div>'
            else:
                result_html = '<div class="error">Nothing pending to cancel.</div>'
            return DEMO_FORM.format(result_html=result_html, prefill=prefill, date_stamp=date_stamp)
        else:
            parsed, error, warnings = build_offer_draft(offer_text, "demo-web")
            if not error:
                save_draft(demo_key, parsed)
                if TX_UNVERIFIED_WARNING in warnings:
                    tx_html = format_tx_confirmation(parsed).replace("\n", "<br>")
                    result_html = f'<div class="result"><div class="result-stamp">Confirm Offer</div><div style="line-height:1.8;color:var(--text-muted);">{tx_html}</div></div>'
                    return DEMO_FORM.format(result_html=result_html, prefill=prefill, date_stamp=date_stamp)
                summary_html = format_offer_confirmation(parsed).replace("\n", "<br>")
                result_html = f'<div class="result"><div class="result-stamp">Confirm Offer</div><div style="line-height:1.8;color:var(--text-muted);">{summary_html}</div></div>'
                return DEMO_FORM.format(result_html=result_html, prefill=prefill, date_stamp=date_stamp)

            existing_draft = get_draft(demo_key)
            if existing_draft:
                correction = parse_correction_sms(offer_text)
                if correction:
                    existing_draft.update(correction)
                    if "price" in correction or "down_payment_pct" in correction:
                        agent = existing_draft.get("agent") or {}
                        existing_draft["down_payment_amount"] = int(existing_draft["price"] * existing_draft["down_payment_pct"])
                        existing_draft["loan_amount"] = existing_draft["price"] - existing_draft["down_payment_amount"]
                        existing_draft["earnest_money"] = int(existing_draft["price"] * agent.get("default_earnest_pct", 0.01))
                    save_draft(demo_key, existing_draft)
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
            record_offer(demo_key, parsed, filename)
            pdf_url = sign_pdf_url(filename)
            _pdf_expires = int(time.time()) + PDF_LINK_TTL
            _pdf_sig = hmac.new(PDF_LINK_SECRET.encode(), f"{filename}:{_pdf_expires}".encode(), hashlib.sha256).hexdigest()[:16]
            close_date_str = ""
            try:
                close_date_str = fmt_closing_date(offer_closing_date(parsed["close_days"]))
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
            share_text = "Drafted a TREC 20-19 by texting the terms. TxtAnOffer turns '725k 3% 21day 123 Main St' into a draft contract to review and finish before signing."
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
                <div class="pdf-mobile"><a href="{pdf_url}" target="_blank">Tap to view your draft contract &rarr;</a></div>
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
                  body: JSON.stringify({{source_id: '{demo_key}', url: url, filename: '{filename}', expires: '{_pdf_expires}', sig: '{_pdf_sig}'}})
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
    close_date = fmt_closing_date(offer_closing_date(parsed["close_days"]))
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
    close_date = fmt_closing_date(offer_closing_date(parsed["close_days"]))
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


# Phones in an app's built-in browser (Facebook/Instagram on Android
# especially) can hand the page a PDF named after its content-provider id
# ("msf:1000034", "document:42") with no ".pdf" at all, and a file picked
# straight from Google Drive/iCloud can arrive as 0 bytes. Found 2026-10-26
# chasing ad visitors who touched the drop box and never got a report:
# the name check alone turned a real contract away. So the bytes decide
# (a PDF starts with "%PDF-", allowed anywhere in the first 1KB), and the
# name is only a fallback for a file we can't peek at.
TC_EMAIL_FALLBACK = "Or email the PDF to tc@check.txtanoffer.com and the report comes back by email."


def _upload_kind(f) -> str:
    """'pdf', 'empty' or 'other' for one uploaded FileStorage."""
    try:
        head = f.stream.read(1024)
        f.stream.seek(0)
    except Exception:
        return "pdf" if f.filename.lower().endswith(".pdf") else "other"
    if not head:
        return "empty"
    return "pdf" if b"%PDF-" in head else "other"


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
    # Deploy checks post source_page=claude_verify (normalized to "unknown"
    # just below): never alert Phanel about our own verification run.
    verify_run = source_page == "claude_verify"
    if source_page not in ("homepage", "tc_check_page"):
        source_page = "unknown"

    # Which state the page's Texas | Colorado switch was on (see
    # _site_state) -- metadata only, so /analytics can tell a Colorado-mode
    # visitor apart without a schema change.
    state_mode = request.form.get("state_mode") if request.form.get("state_mode") in _SITE_STATES else ""

    is_demo = request.form.get("is_demo") == "1"
    if is_demo:
        track_event("tc_check_demo_used", metadata={"source": request.cookies.get("ta_src") or "direct", "source_page": source_page, "state_mode": state_mode,
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
                                                    "state_mode": state_mode, "visitor": request.cookies.get("ta_vid", "")})

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

    # Up to 4 files under the same 'file' field: the contract, and
    # optionally its 40-11 Third Party Financing Addendum, 41-3 Loan
    # Assumption Addendum and/or its 39-11
    # Amendment to Contract, each as a separate PDF (the realistic case --
    # see tc_audit.check_tc_file's docstring for why that's not the same as
    # the FA_-prefix merged-file case).
    uploads = request.files.getlist("file")
    if not uploads or not uploads[0].filename:
        return jsonify({"error": "No file uploaded. Attach a PDF as 'file'."}), 400
    if len(uploads) > 4:
        return jsonify({"error": "Upload at most 4 files: the contract and, if you have them, the 40-11 and 41-3 addenda and/or 39-11 amendment."}), 400
    kinds = [_upload_kind(f) for f in uploads]
    if "empty" in kinds:
        return jsonify({"error": "That file arrived empty (0 bytes). Files picked straight from Google Drive or iCloud "
                                 "inside an app sometimes do: save it to your phone first and pick it again. " + TC_EMAIL_FALLBACK}), 400
    if "other" in kinds:
        return jsonify({"error": "Only PDF files are supported. " + TC_EMAIL_FALLBACK}), 400

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
            return jsonify({"error": "Couldn't read that file as a PDF. Make sure it's not corrupted or password-protected. " + TC_EMAIL_FALLBACK}), 400
    finally:
        for p in tmp_paths:
            try:
                os.remove(p)
            except OSError:
                pass

    # A file we couldn't read (scanned, flattened e-sign export, not a
    # 20-19/CBS1) doesn't use up one of the free reports -- there was no
    # report. It's still tracked as an attempt/tc_check below.
    if not is_demo and result.get("recognized"):
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
    # Nothing to gate on an unreadable file: its only "finding" is that we
    # couldn't read it.
    gate_cleared = is_demo or bool(client["email"]) or free_report or not result.get("recognized")

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
            "source": request.cookies.get("ta_src") or "direct",
            "source_page": source_page,
            "visitor": request.cookies.get("ta_vid", ""),
        })

    # Email Phanel when this is a real outside filled 20-19 (see
    # real_upload_alert.py for exactly what's excluded). Metadata only.
    maybe_send_real_upload_alert(
        result, f"web upload ({source_page})", src_tag=request.cookies.get("ta_src") or "direct",
        sender=client["email"] or submitted_email, is_demo=is_demo, internal=_is_internal_traffic() or verify_run)
    # An unreadable file gets its own alert (S1): it's the likeliest first
    # real file (a flattened e-sign export), and without this nobody sees it.
    maybe_send_unreadable_alert(
        result, f"web upload ({source_page})", src_tag=request.cookies.get("ta_src") or "direct",
        sender=client["email"] or submitted_email, is_demo=is_demo, internal=_is_internal_traffic() or verify_run,
        hand_review_on=bool(hand_review_email()))

    payload = dict(result)
    full_issues = payload.get("issues") or []
    payload["issue_count"] = len(full_issues)
    # Exposed ungated same as issue_count above -- the scan-reveal banner on
    # the frontend leads with this number even before an email is on file,
    # same "tell them enough to know it's real" logic as the itemized count.
    payload["blocker_count"] = sum(1 for i in full_issues if i.get("severity") == "blocker")
    # The gate's "N must-fix items found." headline -- counted on the same
    # folded list the report shows (per-page initials merged), so the number
    # behind the gate matches the "N must fix" the report shows once
    # unlocked. blocker_count above is the raw count (13 on the sample, vs
    # 12 in the report); kept as is for API callers. trec-qa 2026-10-23.
    from tc_check_email import _display_issues as _fold_issues
    payload["must_fix_count"] = sum(1 for i in _fold_issues(full_issues) if i.get("severity") == "blocker")
    if not gate_cleared and full_issues:
        # Reveal how many, never where: the exact section/page/message text
        # is the whole reason an email is worth giving. The "including X, Y"
        # category teaser and the gate's stats line were removed 2026-10-23
        # (trec-qa): the count alone, and no stats claim.
        payload["issues"] = []
        payload["gated"] = True
    else:
        payload["gated"] = False
    # Report-ready view of the same findings (2026-10-07): per-page initials
    # folded into one line, each with its consequence tag -- the exact list
    # the emailed report shows, so the on-screen report and the downloaded
    # PDF match it. Empty while gated, same as "issues".
    from tc_check_email import _display_issues, _consequence_tag, _clean, _files_line, _finding_page, _why_it_matters
    payload["display_issues"] = sorted(
        ({"severity": i.get("severity"), "tag": _consequence_tag(i), "message": _clean(i.get("message", "")),
          # The sample report (2026-10-25) also shows each finding's form
          # page and a "Why it matters" line; real reports don't yet, since
          # only the sample's finding keys have a line written.
          **({"page": _finding_page(i), "why": _why_it_matters(i)} if is_demo else {})}
         for i in _display_issues(payload.get("issues") or [])),
        key=lambda i: i["severity"] != "blocker",  # must-fix first, same order as the email/PDF
    )
    payload["files_line"] = _files_line(result) if result.get("recognized") else ""
    if not is_demo and not client["email"]:
        if result.get("recognized"):
            payload["free_reports_left"] = max(TC_FREE_FULL_REPORTS - client["use_count"] - 1, 0)
        else:
            payload["free_report_not_used"] = True
    # The page shows "Send it, we'll check it by hand." + a send button only
    # when there's an inbox to send it to (see tc_check_email.hand_review_email).
    payload["hand_review"] = bool(not result.get("recognized") and not result.get("unsupported")
                                  and not is_demo and hand_review_email())

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
                send_html_email(to_email, subject_line(res), format_reply_body(res, count), format_reply_html(res, count),
                                reply_to=reply_to_for(res))
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


@app.route("/v1/tc/check/hand-review", methods=["POST"])
def tc_check_hand_review():
    """S1 (2026-10-24): the "Send it, we'll check it by hand." button under
    an unreadable web result. The browser still holds the file it just
    uploaded, so this re-posts it with the visitor's email and we email it,
    attached, to HAND_REVIEW_EMAIL with Reply-To set to the visitor --
    Phanel answers by replying. Nothing is written to disk or the volume
    here; the copy lives in that inbox (the page says so).

    Only reachable from the page when HAND_REVIEW_EMAIL is set (the result
    payload's hand_review flag); 503 otherwise, so a stale page can't claim
    a send that went nowhere. Confirms only after the send succeeds."""
    to = hand_review_email()
    if not to:
        return jsonify({"error": "Hand checks aren't set up yet."}), 503
    client_ip = request.remote_addr or "unknown"
    if not check_and_increment(f"tc_hand_review:{client_ip}", limit=5):
        return jsonify({"error": "Too many requests. Try again in a bit."}), 429

    email = (request.form.get("email") or "").strip()
    if "@" not in email or "." not in email.split("@")[-1] or len(email) > 254:
        return jsonify({"error": "Enter your email so Phanel can write back."}), 400
    uploads = [f for f in request.files.getlist("file") if f and f.filename]
    if not uploads or len(uploads) > 4 or any(_upload_kind(f) != "pdf" for f in uploads):
        return jsonify({"error": "Attach the PDF you just checked."}), 400

    raw_page = request.form.get("source_page") or ""
    source_page = raw_page if raw_page in ("homepage", "tc_check_page") else "unknown"
    src_tag = request.cookies.get("ta_src") or "direct"
    # Our own deploy check: exercise everything except the send itself.
    if raw_page == "claude_verify":
        return jsonify({"ok": True, "verify": True})

    import base64
    attachments = [{"filename": os.path.basename(f.filename), "content": base64.b64encode(f.read()).decode()}
                   for f in uploads]
    subject, text = format_hand_review_request(email, source_page, src_tag, [a["filename"] for a in attachments])
    sent = send_html_email(to, subject, text, "<pre style=\"font-family:sans-serif;white-space:pre-wrap\">" + escape(text) + "</pre>",
                           reply_to=email, attachments=attachments)
    if not sent.get("success"):
        return jsonify({"error": "That didn't send. Try again in a minute."}), 502

    cid = request.cookies.get("tc_cid")
    if cid and not get_tc_client(cid)["email"]:
        save_tc_email(cid, email)
    track_event("tc_hand_review_requested", email.lower(), {
        "files": len(attachments), "source": src_tag, "source_page": source_page,
        "internal": _is_internal_traffic(), "visitor": request.cookies.get("ta_vid", ""),
    })
    return jsonify({"ok": True})


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
    maybe_send_real_upload_alert(result, "email forward", src_tag="email", sender=sender)
    maybe_send_unreadable_alert(result, "email forward", src_tag="email", sender=sender,
                                hand_review_on=bool(hand_review_email()))

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
    # Reply-To is the hand-review inbox on an unreadable file, so "reply
    # with the file attached" reaches a person (tc_check_email.reply_to_for).
    send_html_email(sender, subject_line(result), format_reply_body(result, check_count, archived),
                    format_reply_html(result, check_count, archived), reply_to=reply_to_for(result))
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
    # Redesigned 2026-10-23 (ad-creative mockup, Phanel-approved, copy passed
    # by txo-trec-qa), in the homepage's style with its nav and footer
    # (_site_nav / _SITE_FOOTER). Five sections: the drop box as the hero
    # (it shrinks to one "Check another file" line above a report), the
    # sample, what it checks (#what-it-checks / #colorado), trust, and other
    # ways to check. Every id, data-evt and flow the old page had is kept:
    # ?demo=1, ?carry=1, ?state=co, the 3-free-reports gate, the title-team
    # src line.
    html = """<!DOCTYPE html>
<html lang="en" class="__STATE_CLASS__">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TC File Check — TxtAnOffer</title>
<meta name="description" content="Drop a filled Texas TREC 20-19 or Colorado CBS1 PDF and see what's missing before title kicks it back.">
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
  :root{--green:#0b5d52;--green-deep:#0a3f3a;--tint:#e7f3f1;--ink:#1d1d1f;--mute:#6e6e73;--gray:#f5f5f7;--line:#e3e3e6;--red:#c62828;--amber:#9a6200}
  *{box-sizing:border-box;margin:0;padding:0}
  html{-webkit-font-smoothing:antialiased;scroll-behavior:smooth}
  body{font-family:Inter,-apple-system,system-ui,sans-serif;color:var(--ink);background:#fff;line-height:1.4}
  a{color:inherit;text-decoration:none}
  html.state-tx [data-state="co"],html.state-co [data-state="tx"]{display:none !important}
  input[type=file]{display:none}

  __SITE_CHROME_CSS__

  /* ---------- type scale: homepage's 3 sizes ---------- */
  section{padding:96px 22px;text-align:center}
  .eyebrow{font-size:19px;font-weight:600;color:var(--green);margin-bottom:10px}
  h1{font-size:64px;line-height:1.05;letter-spacing:-.035em;font-weight:800}
  h2{font-size:48px;line-height:1.08;letter-spacing:-.03em;font-weight:800}
  .sub{font-size:21px;line-height:1.4;color:var(--mute);font-weight:500;max-width:640px;margin:16px auto 0}
  .link{font-size:17px;color:var(--green);font-weight:600}
  .link:after{content:" \\203A"}
  .gray{background:var(--gray)}

  /* title-team line (src ti_* / title_*, first_screen.title_line_html) */
  .title-line{max-width:640px;margin:0 auto 26px;background:var(--tint);border-radius:18px;padding:14px 18px;font-size:14px;line-height:1.5;text-align:left}
  .title-link{display:flex;gap:8px;margin-top:10px}
  .title-link input{flex:1;min-width:0;font:13px Inter,sans-serif;padding:8px 12px;border:1px solid #d2d2d7;border-radius:980px;background:#fff}
  .title-link button{font:600 13px Inter,sans-serif;padding:8px 14px;border-radius:980px;border:0;background:var(--green);color:#fff;cursor:pointer}

  /* ---------- 1. hero = the drop box ---------- */
  .hero{padding-top:64px;padding-bottom:88px}
  .state-switch{display:inline-flex;background:#ececf0;border-radius:980px;padding:4px;margin-top:24px}
  .state-switch button{border:0;background:none;font:600 14px Inter,sans-serif;color:#424245;padding:8px 18px;border-radius:980px;cursor:pointer}
  .state-switch button small{font-weight:500;color:var(--mute);margin-left:4px}
  .state-switch button[aria-pressed="true"]{background:#fff;color:var(--ink);box-shadow:0 1px 3px rgba(0,0,0,.12)}
  .state-switch button:focus-visible{outline:3px solid #f5c242;outline-offset:2px}
  .checkbox-wrap{max-width:760px;margin:32px auto 0}
  .drop-zone{border:2px dashed #9cc7bf;background:#fbfdfc;border-radius:28px;padding:56px 24px 44px;cursor:pointer;transition:.15s}
  .drop-zone:hover,.drop-zone.drag{border-color:var(--green);background:var(--tint)}
  .drop-zone.drag *{pointer-events:none}
  .dz-icon{width:64px;height:64px;border-radius:50%;background:var(--green);color:#fff;display:grid;place-items:center;margin:0 auto 18px}
  .dz-title{font-size:28px;font-weight:800;letter-spacing:-.02em}
  .dz-sub{font-size:17px;color:var(--mute);margin-top:6px}
  .dz-sub u{color:var(--green);text-decoration:none;font-weight:600}
  .dz-meta{font-size:13px;color:var(--mute);margin-top:18px}
  .dz-touch,.dz-again{display:none}
  .under{display:flex;flex-wrap:wrap;justify-content:center;align-items:center;gap:10px 26px;margin-top:18px;font-size:14px;color:#424245}
  .under .ok{display:inline-flex;gap:6px;align-items:center}
  .under .ok i{width:18px;height:18px;border-radius:50%;background:var(--green);color:#fff;font:700 11px/18px Inter,sans-serif;text-align:center;font-style:normal}
  .opts{display:flex;flex-wrap:wrap;justify-content:center;gap:12px 22px;margin-top:18px;font-size:14px;color:var(--mute)}
  .email-optin{display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:center}
  .email-optin-check{display:inline-flex;gap:6px;align-items:center;cursor:pointer}
  .email-optin-input{font:15px Inter,sans-serif;padding:9px 14px;border:1px solid #d2d2d7;border-radius:980px;width:230px}
  .email-optin-input:focus,.gate-form input:focus{outline:none;border-color:var(--green)}
  .email-optin-confirm{display:none;width:100%;font-size:13px;color:var(--green);font-weight:600}
  .email-optin-confirm.show{display:block}
  .addendum-toggle{color:var(--green);font-weight:600;cursor:pointer}
  .addendum-row{display:flex;align-items:center;gap:8px;justify-content:center}
  .addendum-row[hidden]{display:none}
  .addendum-filename{color:var(--ink)}
  .addendum-clear{border:0;background:none;font-size:18px;color:var(--mute);cursor:pointer}
  .sample-btn{display:inline-block;margin-top:26px;font:600 17px Inter,sans-serif;color:var(--green);background:#fff;border:1.5px solid var(--green);border-radius:980px;padding:12px 24px;cursor:pointer}
  .sample-btn:disabled{opacity:.7;cursor:default}

  /* uploading: #status */
  .status{display:none}
  .status.show{display:block;margin-top:22px}
  .up-file{display:inline-flex;align-items:center;gap:12px;background:var(--gray);border-radius:14px;padding:12px 18px;font-size:15px;max-width:100%}
  .up-file b{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .up-size{color:var(--mute)}
  .spin{flex:none;width:18px;height:18px;border-radius:50%;border:2.5px solid #cfe3df;border-top-color:var(--green);animation:sp 1s linear infinite}
  @keyframes sp{to{transform:rotate(360deg)}}
  .up-step{font-size:15px;color:var(--mute);margin:12px 0 6px}

  /* a report on screen: the box shrinks to one line, options hide */
  .has-result .drop-zone{padding:18px 22px;display:flex;align-items:center;justify-content:center;gap:12px;border-radius:18px}
  .has-result .dz-icon{width:34px;height:34px;margin:0}
  .has-result .dz-icon svg{width:18px;height:18px}
  .has-result .dz-title{font-size:17px}
  .has-result .dz-first,.has-result .dz-sub,.has-result .dz-meta,.has-result .pre-result{display:none}
  .result-fallback{font-size:14px;color:var(--mute);margin-top:10px}
  .result-fallback a{color:var(--green);text-decoration:underline}
  .has-result .dz-again{display:inline}

  /* result: same card language as the homepage hero report */
  /* scroll-margin-top: revealResult() scrolls this to block:'start', which put the
     demo banner and report header under the 52px sticky nav at 375px. */
  .result{display:none;text-align:left;max-width:760px;margin:28px auto 0;scroll-margin-top:68px}
  .result.show{display:block}
  .demo-banner{font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--mute);margin-bottom:10px}
  .rep{background:#fff;border-radius:22px;overflow:hidden;box-shadow:0 30px 70px -28px rgba(10,63,58,.35),0 0 0 1px rgba(0,0,0,.05)}
  .rep-head{background:var(--green-deep);display:flex;justify-content:space-between;align-items:center;padding:15px 22px}
  .rep-head img{height:17px;width:auto}
  .rep-kick{font-size:11px;font-weight:700;letter-spacing:.1em;color:#cfe3df;text-transform:uppercase}
  .rep-body{padding:20px 22px 8px}
  .rep-addr{font-size:22px;font-weight:700;letter-spacing:-.01em}
  .rep-addr.blank{color:var(--red)}
  .rep-sub{font-size:13px;color:var(--mute);margin-top:2px}
  .meta-bar{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12.5px;color:var(--mute);margin-top:12px}
  .meta-bar strong{color:var(--ink);word-break:break-all}
  .result-banner{border-radius:12px;padding:12px 15px;margin:14px 0 6px;font-size:15px;font-weight:700}
  .result-banner.incomplete{background:#fdecec;color:var(--red)}
  .result-banner.complete{background:var(--tint);color:var(--green)}
  .rep-counts{display:flex;gap:12px;font-size:13px;margin-top:2px}
  .rep-counts .review{color:var(--amber)}
  .grp{font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--mute);margin:16px 0 2px}
  .issue-list{list-style:none}
  .issue-item{display:grid;grid-template-columns:200px 1fr;gap:14px;padding:11px 0;border-bottom:1px solid var(--line);font-size:14px;line-height:1.45}
  .issue-item:last-child{border-bottom:0}
  .issue-page{display:block;margin-top:4px;font-size:12px;font-weight:700;color:var(--mute)}
  .issue-why{display:block;margin-top:4px;font-size:13px;color:var(--mute)}
  .issue-why b{color:var(--ink)}
  .issue-tag{font-size:10.5px;font-weight:800;letter-spacing:.04em;padding:4px 8px;border-radius:6px;height:fit-content;width:fit-content}
  .issue-tag.blocker{background:#fdecec;color:var(--red)}
  .issue-tag.warning,.issue-tag.info{background:#fff4dc;color:var(--amber)}
  .rep-actions{display:flex;flex-wrap:wrap;gap:10px;padding:16px 22px 20px;border-top:1px solid var(--line)}
  .pill{font:600 14px Inter,sans-serif;padding:9px 16px;border-radius:980px;border:1.5px solid #d2d2d7;background:#fff;color:var(--ink);cursor:pointer}
  .pill.solid{background:var(--green);border-color:var(--green);color:#fff}
  .pill:disabled{opacity:.7}
  .fixit-cta{background:#fff4dc;border-radius:12px;padding:12px 15px;margin:10px 0;font-size:14px}
  .fixit-cta a{color:var(--green);font-weight:600}
  .own-file-cta,.next-step-cta{margin-top:18px;background:var(--tint);border-radius:18px;padding:20px 22px;font-size:15px;line-height:1.5}
  .own-file-cta a,.next-step-lead a{color:var(--green);font-weight:600}
  .own-file-btn{margin-top:12px}
  .agent-alt{font-size:13px;color:var(--mute);margin-top:10px}
  .agent-alt a{text-decoration:underline;color:var(--mute);font-weight:500}
  .unreadable-box{background:var(--gray);border-radius:18px;padding:18px 20px;margin-top:14px;font-size:15px;line-height:1.5;color:#424245}
  .unreadable-box p+p{margin-top:8px}
  .unreadable-title{font-weight:700;color:var(--ink)}
  .unreadable-note{color:var(--green);font-weight:600}
  /* gate (and the optional "email me this report" ask, same box) */
  .gate-box{background:#fff;border-radius:22px;padding:26px 24px;margin-top:14px;box-shadow:0 0 0 1px rgba(0,0,0,.06);text-align:center}
  .gate-headline{font-size:22px;font-weight:800;letter-spacing:-.02em}
  .gate-sub{font-size:15px;color:var(--mute);margin:8px auto 0;max-width:520px}
  .gate-form{display:flex;gap:10px;justify-content:center;margin-top:16px;flex-wrap:wrap}
  .gate-form input{font:15px Inter,sans-serif;padding:11px 16px;border:1px solid #d2d2d7;border-radius:980px;width:260px}
  .gate-error{font-size:13px;color:var(--red);margin-top:8px;min-height:1px}
  .gate-note{font-size:13px;color:var(--mute);margin-top:12px}
  .gate-bridge{font-size:14px;color:var(--mute);margin-top:14px;text-align:center}
  .gate-bridge a{color:var(--green);font-weight:600}
  .locked{filter:blur(4px);opacity:.55;pointer-events:none;user-select:none}

  /* ---------- 2. sample ---------- */
  .sample-strip{max-width:620px;margin:40px auto 0;text-align:left;cursor:pointer;background:#fff;border-radius:22px;padding:6px 22px 4px;
                box-shadow:0 30px 70px -30px rgba(10,63,58,.3),0 0 0 1px rgba(0,0,0,.04);transition:transform .15s}
  .sample-strip:hover{transform:translateY(-2px)}
  .sample-strip:focus-visible{outline:3px solid #f5c242;outline-offset:2px}
  .ss-label{font-size:13px;color:var(--mute);padding:14px 0 6px}
  .sample-strip ul{list-style:none}
  .sample-strip li{display:flex;gap:12px;padding:13px 0;border-top:1px solid var(--line);font-size:15px;line-height:1.45}
  .ss-tag{flex:none;font-size:10.5px;font-weight:800;letter-spacing:.04em;padding:4px 8px;border-radius:6px;height:fit-content}
  .ss-tag.b{background:#fdecec;color:var(--red)}
  .ss-tag.w{background:#fff4dc;color:var(--amber)}
  .ss-go{display:block;text-align:center;font-weight:600;color:var(--green);padding:14px 0 12px;border-top:1px solid var(--line);font-size:15px}

  /* ---------- 3. what it checks ---------- */
  .scope{max-width:1024px;margin:44px auto 0;text-align:left}
  .scope ul{list-style:none;display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
  .scope li{background:var(--gray);border-radius:16px;padding:16px 18px;font-size:14.5px;line-height:1.45;font-weight:500}
  .scope .not{margin-top:22px;font-size:14px;color:var(--mute);text-align:center}
  .scope .not b{color:var(--ink)}
  .scope-footnote{font-size:14px;color:var(--mute);text-align:center;margin-top:12px}
  .scope-footnote a{color:var(--green);font-weight:600}

  /* ---------- 4. trust ---------- */
  .facts{display:flex;justify-content:center;gap:48px;margin-top:36px;font-size:17px;font-weight:600;flex-wrap:wrap}
  .facts span{display:flex;align-items:center;gap:8px}
  .facts i{flex:none;width:22px;height:22px;border-radius:50%;background:var(--green);color:#fff;font-style:normal;font-size:12px;display:grid;place-items:center}
  .privacy-note{font-size:14px;color:var(--mute);max-width:620px;margin:26px auto 0;line-height:1.55}

  /* ---------- 5. other ways: 4 small tiles ---------- */
  .ways{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;max-width:1024px;margin:44px auto 0;text-align:left}
  .way{background:var(--gray);border-radius:22px;padding:26px 22px;display:flex;flex-direction:column;gap:10px}
  .way b{font-size:17px}
  .way p{font-size:14.5px;color:var(--mute);line-height:1.45;flex:1}
  .way a{font-size:15px;color:var(--green);font-weight:600;overflow-wrap:anywhere}
  .learn{margin-top:26px;font-size:15px;color:var(--mute)}
  .learn a{color:var(--green);font-weight:600;margin:0 8px}

  @media (hover:none){.dz-mouse{display:none}.dz-touch{display:inline}}
  @media (max-width:734px){
    section{padding:64px 20px}
    .hero{padding-top:36px;padding-bottom:56px}
    .eyebrow{font-size:16px}
    h1{font-size:38px}
    h2{font-size:32px}
    .sub{font-size:17px}
    .state-switch{margin-top:18px}
    .state-switch button{padding:8px 14px;font-size:13px}
    .checkbox-wrap{margin-top:22px}
    .drop-zone{padding:34px 18px 28px;border-radius:22px}
    .dz-icon{width:54px;height:54px;margin-bottom:14px}
    .dz-title{font-size:22px}
    .dz-mouse{display:none}.dz-touch{display:inline}
    .has-result .dz-touch{display:none}
    .under{gap:8px 16px;font-size:13px}
    .email-optin-input{width:100%}
    .sample-btn{font-size:16px;margin-top:20px}
    .issue-item{grid-template-columns:1fr;gap:6px}
    .rep-body{padding:16px 16px 6px}.rep-actions{padding:14px 16px 16px}
    .gate-headline{font-size:19px}
    .gate-form input{width:100%}
    .scope ul{grid-template-columns:1fr;gap:8px}
    .scope li{padding:12px 14px;font-size:14px}
    .ways{grid-template-columns:1fr}
    .facts{flex-direction:column;align-items:center;gap:14px}
  }
</style>
</head>
<body>
__SITE_NAV__
<!-- 1. HERO: the drop box is the page. The title-team line
     (__FS_TITLE_LINE__, src ti_*/title_* only) sits above the H1. -->
<section class="hero" id="check">
  __FS_TITLE_LINE__
  <div class="eyebrow">__FS_TC_EYEBROW__</div>
  <h1><span data-state="tx">__FS_TC_H1__</span><span data-state="co">__FS_TC_H1_CO__</span></h1>
  <p class="sub"><span data-state="tx">__FS_TC_SUB_TX__</span><span data-state="co">__FS_TC_SUB_CO__</span></p>
  <div class="state-switch" role="group" aria-label="Which state's contract">
    <button type="button" data-set-state="tx" aria-pressed="__TX_PRESSED__" data-evt="state_toggle_tx">Texas <small>TREC 20-19</small></button>
    <button type="button" data-set-state="co" aria-pressed="__CO_PRESSED__" data-evt="state_toggle_co">Colorado <small>CBS1</small></button>
  </div>
  __STATE_SWITCH_JS__

  <div class="checkbox-wrap" id="checkWrap">
    <div class="drop-zone" id="dropZone">
      <div class="dz-icon"><svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 13v8"/><path d="m8 17 4-4 4 4"/><path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3"/></svg></div>
      <div class="dz-title"><span class="dz-first"><span class="dz-mouse">Drop your contract PDF here</span><span class="dz-touch">Choose your contract PDF</span></span><span class="dz-again">Check another file</span></div>
      <div class="dz-sub"><span class="dz-mouse">or <u>click to browse</u></span><span class="dz-touch"><u>Tap to browse files</u></span></div>
      <div class="dz-meta"><span data-state="tx">Fillable TREC 20-19 PDF</span><span data-state="co">Fillable CBS1 (2026) PDF</span> &middot; not scanned or flattened files &middot; flags likely issues, not legal advice</div>
    </div>
    <input type="file" id="fileInput" accept="application/pdf,.pdf">

    <!-- Uploading: #status, the same generic step labels as before (no fake stages) -->
    <div class="status" id="status"></div>

    <!-- Before a report: one line of reassurance, one of options, one secondary action -->
    <div class="pre-result">
      <div class="under">
        <span class="ok"><i>&#10003;</i>3 free reports, no signup</span>
        <span class="ok"><i>&#10003;</i>Results in seconds</span>
        <span class="ok"><i>&#10003;</i>Checked, then deleted. No AI reads your contract.</span>
      </div>
      <div class="opts">
        <div class="email-optin">
          <label class="email-optin-check"><input type="checkbox" id="emailOptinCheckbox" checked> Email me this report + future checks for this address</label>
          <input type="email" id="emailOptinInput" class="email-optin-input" placeholder="you@example.com" autocomplete="email">
          <div class="email-optin-confirm" id="emailOptinConfirm"></div>
        </div>
      </div>
      <div class="opts">
        <div class="addendum-toggle" id="addendumToggle"><span data-state="tx">+ Also have the 40-11, 41-3 or 39-11? Add up to 3 to cross-check them.</span><span data-state="co">+ Also have the buyer&rsquo;s Exclusive Right-to-Buy, an Amend/Extend, Counterproposal, Inspection Objection Notice or Source of Water Addendum? Add up to 3 to check them against the CBS1.</span></div>
        <div class="addendum-row" id="addendumRow" hidden><input type="file" id="addendumInput" accept="application/pdf,.pdf" multiple><span class="addendum-filename" id="addendumFileName"></span><button type="button" class="addendum-clear" id="addendumClear" title="Remove">&times;</button></div>
      </div>
      <button type="button" class="sample-btn demo-check-btn" id="demoBtn" data-evt="tc_sample_btn">__FS_TC_SAMPLE_BTN__</button>
    </div>

    <div class="result" id="result"></div>
  </div>
</section>

<!-- 2. SAMPLE: tapping the strip runs the real sample check (sampleStrip -> demoBtn.click(), counts as tc_sample_btn) -->
<section class="gray">
  <div class="eyebrow">Sample report</div>
  <h2>No PDF handy?<br>Run the sample.</h2>
  <p class="sub">A sample contract with planted mistakes. Not a client file.</p>
  <div class="sample-strip" id="sampleStrip" role="button" tabindex="0" aria-label="Findings from TC Check's sample contract. Open the full sample report.">
    <div data-state="tx"><p class="ss-label">__FS_TC_STRIP_LABEL_TX__</p><ul>__FS_STRIP_TX__</ul></div>
    <div data-state="co"><p class="ss-label">__FS_TC_STRIP_LABEL_CO__</p><ul>__FS_STRIP_CO__</ul></div>
    <span class="ss-go">See the full sample report &rarr;</span>
  </div>
</section>

<!-- 3. WHAT IT CHECKS: #what-it-checks wraps both states (the homepage links here) -->
<section id="what-it-checks">
  <h2>What it checks.</h2>
  <div data-state="tx">
    <p class="sub">Each check reads a specific field of TREC&rsquo;s published 20-19, its position confirmed by rendering the form.</p>
    <div class="scope">
      <ul>
        <li>Property address, county &mdash; and the address header on every page</li>
        <li>Buyer &amp; Seller legal name</li>
        <li>Earnest money, option fee, option period days</li>
        <li>Escrow agent, title company</li>
        <li>Effective Date, and a closing date that&rsquo;s real and after it</li>
        <li>Initials on pages 1&ndash;9: one per buyer and seller, from the right person</li>
        <li>Sales price math: 3A + 3B = 3C</li>
        <li>&ldquo;Check one box only&rdquo; conflicts (survey, Seller&rsquo;s Disclosure, As Is, possession, owners association)</li>
        <li>Seller&rsquo;s Disclosure delivery days and 12B broker contributions filled in</li>
        <li>Page 12 receipts match the option fee and earnest money</li>
        <li>40-11 addendum loan amount, and Third Party Financing listed and attached</li>
        <li>Business terms written into Special Provisions (Paragraph 11)</li>
        <li>Notice email addresses that would bounce</li>
      </ul>
      <p class="not"><b>Not checked:</b> signatures &middot; TREC forms not listed here &middot; scanned or flattened PDFs</p>
      <p class="scope-footnote">The 40-11 can be its own separate PDF &mdash; it doesn&rsquo;t need to be merged into the contract file.</p>
    </div>
  </div>
  <div data-state="co" id="colorado">
    <p class="sub">Checked against the Colorado Real Estate Commission&rsquo;s published CBS1 (mandatory since January 1, 2026), field by field.</p>
    <div class="scope">
      <ul>
        <li>Property street, city, county &mdash; and that the state is Colorado</li>
        <li>Buyer &amp; Seller names, contract date</li>
        <li>&sect;4.1 price table: Earnest Money + loan + Cash at Closing = Purchase Price, and both TOTAL lines</li>
        <li>Earnest Money amount and Earnest Money Holder</li>
        <li>Loan amount vs. loan type checked (&sect;4.5.3), and Available Funds (&sect;4.4.3)</li>
        <li>Blank or N/A deadlines that delete a contingency under &sect;3.2: loan, appraisal, inspection, title objection</li>
        <li>&sect;3.1 deadlines out of order (delivery &rarr; objection &rarr; resolution) or after the Closing Date</li>
        <li>Closing Date blank, on a weekend, or on a federal or Colorado holiday</li>
        <li>Possession before closing, Acceptance Deadline after closing</li>
        <li>Both boxes checked where only one applies, and two payers on one &sect;15 fee</li>
        <li>&sect;29 compensation lines checked with no % or $</li>
        <li>Lead-based paint deadlines left blank</li>
        <li>Amend/Extend and Counterproposal: the contract re-checked as amended &mdash; a new date that lands after closing, a deleted loan or inspection deadline, conflicting boxes, a price change</li>
        <li>Inspection Objection Notice dated after the Inspection Objection Deadline, or with no description</li>
        <li>Attached forms that name a different property or contract date; a Source of Water Addendum listed in &sect;31 but not attached</li>
        <li>HB26-1426: the CBS1 marks a buyer&rsquo;s agent or transaction-broker but no Exclusive Right-to-Buy is attached (signatures aren&rsquo;t checked); an attached one whose Listing Period doesn&rsquo;t cover the contract date, names a different buyer, or states no compensation</li>
      </ul>
      <p class="not"><b>Not checked:</b> signatures &middot; Foreclosure CBS (CBSF1) and Colorado forms not listed here &middot; scanned or flattened PDFs</p>
      <p class="scope-footnote">Flags likely issues; not legal advice. Not affiliated with the Colorado Real Estate Commission.</p>
    </div>
  </div>
  <p class="scope-footnote" style="margin-top:26px">Comparing an original against a later version? <a href="/tc-check/compare">See exactly what changed &rarr;</a></p>
</section>

<!-- 4. TRUST: the privacy lines in one section. Claims are on /trust and
     true of the code: a web upload goes to a temp file that's deleted after
     the check, and TC Check is rule code with no model/LLM dependency. -->
<section class="gray">
  <h2>Checked.<br>Then deleted.</h2>
  <p class="sub">No AI reads your contract.</p>
  <div class="facts">
    <span><i>&#10003;</i>Independent. Not owned by Zillow, a brokerage or an MLS.</span>
    <span><i>&#10003;</i>We don&rsquo;t sell data.</span>
  </div>
  <div class="privacy-note">Your PDF is written to a temporary file, checked, then deleted &mdash; normally within seconds (bulk uploads: when the batch finishes). On the Brokerage plan, contracts forwarded from your brokerage&rsquo;s emails go to its archive (on by default; the brokerage can turn it off).</div>
  <p style="margin-top:22px"><a class="link" href="/trust#contracts" data-evt="tc_trust_link">How we handle files</a></p>
</section>

<!-- 5. OTHER WAYS: everything secondary, one tile each -->
<section>
  <h2>Other ways to check.</h2>
  <div class="ways">
    <div class="way"><b>Forward it</b><p>Forward it to tc@check.txtanoffer.com and the report comes back by email.</p><a href="mailto:tc@check.txtanoffer.com">tc@check.txtanoffer.com</a></div>
    <div class="way"><b>Walk through it together</b><p>A free 15-minute call to walk through the report on one of your files, or on the sample.</p><a href="https://calendly.com/pejeanbaptiste/15-min-file-check?utm_source=txtanoffer&amp;utm_content=tc_check" target="_blank" rel="noopener" data-evt="book_call_tccheck">Book a free 15-min file check &rarr;</a></div>
    <div class="way" data-state="tx"><b>A whole closed-file archive</b><p>Zip up to 20 closed files for a free report by email (200 with a Brokerage join code).</p><a href="/tc-check/bulk">Bulk check &rarr;</a></div>
    <div class="way" data-state="co"><b>Colorado deadlines</b><p>Work out a CBS1 &sect;3.1 deadline schedule.</p><a href="/tools/colorado-deadlines" data-evt="states_co_tool">Deadline calculator &rarr;</a></div>
    <div class="way"><b>Checklists and form references</b><p>Want checklists and TREC form references instead?</p><a href="/tc-hub">Visit the free TC Hub &rarr;</a></div>
  </div>
  <p class="learn">Questions? <a href="/faq">FAQ</a> &middot; <a href="/guides">Guides</a> &middot; <a href="mailto:support@txtanoffer.com">support@txtanoffer.com</a></p>
</section>

__SITE_FOOTER__
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
// Wraps the drop box, its options and the report. 'has-result' shrinks the
// box to one "Check another file" line above a report (2026-10-23 redesign);
// uploadFile() and resetForm() take it off again.
const checkWrap = document.getElementById('checkWrap');
const stateMode = () => (window.taState ? window.taState() : 'tx');

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
    // Same per-state sample as the homepage (see index()).
    const sample = stateMode() === 'co'
      ? { url: '/static/sample_cbs1.pdf?v=2026-10-12', name: 'sample_cbs1.pdf' }
      : { url: '/static/sample_trec_20-19.pdf?v=2026-10-09', name: 'sample_trec_20-19.pdf' };
    fetch(sample.url)
      .then(r => r.blob())
      .then(blob => {
        uploadFile(new File([blob], sample.name, {type: 'application/pdf'}), '', true);
      })
      .catch(() => demoDone(false));
  });
  // Arrived here via the homepage's "+N more -- see the full checklist"
  // link on a demo preview: replay the same sample check immediately
  // instead of landing on an empty form the visitor has to re-trigger by
  // hand.
  if (new URLSearchParams(location.search).get('demo') === '1') demoBtn.click();
  // The sample strip is the same sample report, so a tap on it runs the
  // real check (and counts as tc_sample_btn, via the button's click).
  const sampleStrip = document.getElementById('sampleStrip');
  if (sampleStrip) {
    sampleStrip.addEventListener('click', () => { if (!demoBtn.disabled) demoBtn.click(); });
    sampleStrip.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (!demoBtn.disabled) demoBtn.click(); }
    });
  }
}
// Title-team line (src ti_* / title_* only, first_screen.py): copies the one
// generic share link, same for every title company.
const titleCopyBtn = document.getElementById('titleCopyBtn');
if (titleCopyBtn) {
  const titleLinkUrl = document.getElementById('titleLinkUrl');
  const copyLabel = titleCopyBtn.textContent;
  const copied = () => { titleCopyBtn.textContent = titleCopyBtn.dataset.done; setTimeout(() => { titleCopyBtn.textContent = copyLabel; }, 2000); };
  titleCopyBtn.addEventListener('click', () => {
    const fallback = () => { titleLinkUrl.select(); try { document.execCommand('copy'); copied(); } catch (e) {} };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(titleLinkUrl.value).then(copied, fallback);
    else fallback();
  });
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
    pendingAddendumFiles = Array.from(addendumInput.files).slice(0, 3);
    addendumFileName.textContent = pendingAddendumFiles.map(f => f.name).join(', ');
  }
});
// Texas and Colorado take different companion forms; drop anything already
// picked when the state changes so a TREC addendum can't ride along on a
// CBS1 check (or the reverse).
document.addEventListener('ta:state', () => {
  if (pendingAddendumFiles.length) addendumClear.click();
});
addendumClear.addEventListener('click', () => {
  pendingAddendumFiles = [];
  addendumInput.value = '';
  addendumFileName.textContent = '';
  addendumRow.hidden = true;
  addendumToggle.hidden = false;
});

// Purely a perceived-progress readout for a request that's actually one
// round trip -- the backend doesn't stream distinct stages back. Labeled
// generically (not "AI analyzing..." theater) and never blocks: whichever
// line is showing when the real response lands, that's when it finishes.
const STATUS_STEPS_BY_STATE = {
  tx: ['Scanning TREC 20-19...', 'Checking mandatory fields...', 'Checking initials & consistency...'],
  co: ['Scanning CBS1...', 'Checking \u00a73.1 deadlines...', 'Checking \u00a74.1 price math...'],
};
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
// Measured after .drop-zone's .15s transition: 'has-result' shrinks its
// padding, and scrolling mid-shrink landed ~26px too far at 375px, putting
// the demo banner under the sticky nav (.result's scroll-margin-top clears
// the nav itself).
function revealResult() {
  setTimeout(() => {
    const r = resultEl.getBoundingClientRect();
    if (r.top < 20 || r.top > window.innerHeight * 0.6) resultEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, 200);
}
let pendingAddendumFiles = [];
// The exact files of the last check, kept in the browser only, so an
// unreadable result's "send it for a hand check" button can send them
// without asking the visitor to pick them again.
let lastCheckedFiles = [];

function uploadFile(file, email, isDemo) {
  pendingFile = file;
  lastCheckedFiles = [file].concat(isDemo ? [] : pendingAddendumFiles);
  resultEl.classList.remove('show');
  emailOptinConfirm.classList.remove('show');
  checkWrap.classList.remove('has-result');
  statusTimers.forEach(clearTimeout);
  const STATUS_STEPS = STATUS_STEPS_BY_STATE[stateMode()];
  statusEl.innerHTML = '<div class="up-file"><span class="spin"></span><b>' + escapeHtml(file.name) + '</b><span class="up-size">' + formatBytes(file.size) + '</span></div><div class="up-step"></div>';
  const stepEl = statusEl.querySelector('.up-step');
  statusTimers = STATUS_STEPS.map((label, i) =>
    setTimeout(() => { stepEl.textContent = label; }, i * 450)
  );
  stepEl.textContent = STATUS_STEPS[0];
  statusEl.classList.add('show');

  const formData = new FormData();
  formData.append('file', file);
  formData.append('source_page', 'tc_check_page');
  formData.append('state_mode', stateMode());
  // A real addendum picked for a prior real upload should never ride along
  // on a demo run -- the sample file is the whole check, on its own.
  if (!isDemo) pendingAddendumFiles.forEach(f => formData.append('file', f));
  if (email) formData.append('email', email);
  if (isDemo) formData.append('is_demo', '1');

  // Why an upload never became a report, as a beacon (window.taEvt, from
  // _ENGAGEMENT_JS): a request that dies on a phone's network never reaches
  // /v1/tc/check, so the server alone can't tell "never picked a file" from
  // "picked one and it failed". Real files only, not the sample.
  const beacon = t => { if (!isDemo && window.taEvt) window.taEvt(t); };
  beacon('upload_picked');
  // A stalled upload on a weak mobile connection otherwise spins forever.
  const ctl = window.AbortController ? new AbortController() : null;
  const killer = ctl ? setTimeout(() => ctl.abort(), 120000) : null;
  let failReason = 'upload_failed_network';

  fetch('/v1/tc/check', { method: 'POST', body: formData, signal: ctl ? ctl.signal : undefined })
    .then(r => r.json().catch(() => {
      // An HTML error page (proxy timeout, 413, 502) instead of our JSON.
      failReason = 'upload_failed_server';
      throw new Error('HTTP ' + r.status);
    }))
    .then(data => {
      clearTimeout(killer);
      statusTimers.forEach(clearTimeout);
      statusEl.classList.remove('show');
      if (data.error) {
        beacon('upload_failed_rejected');
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
    .catch(err => {
      clearTimeout(killer);
      statusTimers.forEach(clearTimeout);
      statusEl.classList.remove('show');
      if (err && err.name === 'AbortError') failReason = 'upload_failed_timeout';
      beacon(failReason);
      renderError(failReason === 'upload_failed_timeout'
        ? 'The upload timed out. A weak signal is the usual cause: try again on Wi-Fi.'
        : failReason === 'upload_failed_server'
          ? 'Something went wrong on our end checking that file. Try again.'
          : 'The upload didn\u2019t go through. Check your connection and try again.', true);
      if (isDemo) demoDone(false);
      revealResult();
    });
}

function buildMetaBar(data, file) {
  let html = '<div class="meta-bar">';
  html += '<span><strong>' + escapeHtml(file.name) + '</strong></span>';
  html += '<span>' + formatBytes(file.size) + '</span>';
  if (typeof data.page_count === 'number') html += '<span>' + data.page_count + ' page' + (data.page_count === 1 ? '' : 's') + '</span>';
  html += '<span>' + ((data.recognized || data.unsupported) ? (data.form ? escapeHtml(data.form) + ' detected' : 'TREC 20-19 AcroForm detected') : 'Form fields not readable') + '</span>';
  if (data.has_addendum) html += '<span>40-11 addendum attached</span>';
  if (data.has_loan_assumption) html += '<span>41-3 addendum attached</span>';
  if (data.has_amendment) html += '<span>39-11 amendment attached</span>';
  html += '</div>';
  return html;
}

function formatBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  return (n / (1024 * 1024)).toFixed(1) + ' MB';
}

// withFallback: for a failure the server never explained (network, timeout,
// our own 5xx). Server error messages already end with the email route.
function renderError(msg, withFallback) {
  resultEl.innerHTML = '<div class="result-banner incomplete">' + escapeHtml(msg) + '</div>'
    + (withFallback ? '<p class="result-fallback">Still stuck? Email the PDF to <a href="mailto:tc@check.txtanoffer.com">tc@check.txtanoffer.com</a> and the report comes back by email.</p>' : '');
  resultEl.classList.add('show');
  checkWrap.classList.add('has-result');
}

// Next steps after a report (2026-10-13). The agent SMS-drafting pitch that
// used to sit here (buildUpsellCta) was aimed at the wrong buyer -- TCs
// check files, they don't write offers -- and made claims the product
// doesn't back (that drafted fields are never blank, when drafts leave
// names, earnest money and financing blank on purpose). The primary step is
// now the TC's own next file: upload it here, or forward it by email. The
// agent offer survives only as one small link.
function formLabel(data) { return data.state === 'CO' ? 'CBS1' : 'TREC 20-19'; }

function buildOwnFileCta(data) {
  return '<div class="own-file-cta">' +
    '<p class="own-file-lead"><strong>That was our sample. Now run one of yours.</strong> Drop a filled ' + formLabel(data) + ' above, or forward it to <a href="mailto:tc@check.txtanoffer.com">tc@check.txtanoffer.com</a>. It&rsquo;s checked, then deleted, unless your brokerage archives it.</p>' +
    '<button type="button" class="pill solid own-file-btn" onclick="pickOwnFile()" data-evt="tc_own_file_cta">Check my own file &rarr;</button>' +
    buildAgentAltLink() +
  '</div>';
}

function buildForwardNextCta() {
  return '<div class="own-file-cta">' +
    '<p class="own-file-lead"><strong>Next file?</strong> Forward it to <a href="mailto:tc@check.txtanoffer.com">tc@check.txtanoffer.com</a> straight from your inbox and the report comes back by email.</p>' +
    buildAgentAltLink() +
  '</div>';
}

function buildAgentAltLink() {
  return '<p class="agent-alt">Agent writing the offer? <a href="/demo">Draft the next one by text &rarr;</a></p>';
}

function pickOwnFile() {
  dropZone.scrollIntoView({ behavior: 'smooth', block: 'center' });
  fileInput.click();
}

// Shown once a report has actually been delivered (email on file, or a
// clean/complete result) -- the point where the old flow just went dead.
// Two guided next steps instead of a static stop: run another file right
// here, or route the next one by email. The Brokerage pitch that used to
// sit here was removed 2026-10-02; the agent SMS pitch was cut to one
// small link 2026-10-13 (see buildForwardNextCta above).
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
    '<div class="gate-form"><input type="email" id="gateEmailInput" placeholder="work email" autocomplete="email" onkeydown="if(event.key===\\'Enter\\')unlockGate()"><button type="button" class="pill solid" onclick="unlockGate()">Email me this report</button></div>' +
    '<div class="gate-error" id="gateError"></div>' +
    '<div class="gate-note">' + leftText + ' Your file is deleted after the check.</div>' +
  '</div>';
}

function resetForm() {
  checkWrap.classList.remove('has-result');
  resultEl.classList.remove('show');
  resultEl.innerHTML = '';
  pendingFile = null;
  pendingAddendumFiles = [];
  fileInput.value = '';
  if (addendumInput) addendumInput.value = '';
  dropZone.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

// The report card (2026-10-23 redesign): the same card language as the
// homepage's sample card -- green head, then address, meta bar, banner and
// the findings grouped "Must fix" / "To review".
function buildReportHead() {
  return '<div class="rep-head"><img src="/static/logo-wordmark-white.png?v=1" alt="txtanoffer"><span class="rep-kick">TC File Check report</span></div>';
}

function buildReportAddress(data) {
  const p = data.property || {};
  let html = p.address
    ? '<div class="rep-addr">' + escapeHtml(p.address) + '</div>'
    : '<div class="rep-addr blank">Property address not filled in</div>';
  const bits = [];
  if (p.address) { if (p.city) bits.push(escapeHtml(p.city)); if (p.county) bits.push(escapeHtml(p.county) + ' County'); }
  else bits.push(data.form === 'TREC 9-18' ? 'Section 2 is blank' : 'Section 2A is blank');  // 9-18 has no 2A
  if (data.files_line) bits.push(escapeHtml(data.files_line));
  html += '<div class="rep-sub">' + bits.join(' &middot; ') + '</div>';
  return html;
}

function buildIssueGroups(issues) {
  const groups = [['Must fix', issues.filter(i => i.severity === 'blocker')],
                  ['To review', issues.filter(i => i.severity !== 'blocker')]];
  let html = '';
  for (const [label, list] of groups) {
    if (!list.length) continue;
    html += '<div class="grp">' + label + '</div><ul class="issue-list">';
    for (const issue of list) {
      // page/why come only on the sample report (see /tc-check's payload).
      let extra = '';
      if (issue.page) extra += '<span class="issue-page">' + label + ' &middot; ' + escapeHtml(issue.page) + '</span>';
      if (issue.why) extra += '<span class="issue-why"><b>Why it matters:</b> ' + escapeHtml(issue.why) + '</span>';
      html += '<li class="issue-item"><span class="issue-tag ' + issue.severity + '">' + escapeHtml(issue.tag || issue.severity) + '</span><span>' + escapeHtml(issue.message) + extra + '</span></li>';
    }
    html += '</ul>';
  }
  return html;
}

// A file TC Check couldn't read (scanned, flattened e-sign export, or not
// the form) gets a neutral grey note -- never the red "Not ready" banner,
// a blocker tag or a count, since nothing was found wrong with the
// contract. Worded for the state the visitor picked, so a Colorado visitor
// isn't told about the TREC 20-19.
function buildUnreadable(data, formName) {
  if (data.hand_review) return buildHandReview(data, formName);
  return '<div class="unreadable-box">' +
    '<p class="unreadable-title">We couldn&rsquo;t read the form fields in this file.</p>' +
    '<p>It may be scanned, or flattened when it was signed or exported, or it may not be a ' + formName + '. That isn&rsquo;t a finding about your contract: TC Check needs a fillable ' + formName + ' PDF (addenda are checked alongside it).</p>' +
    '<p>Try an unsigned copy exported from your forms software.</p>' +
    (data.free_report_not_used ? '<p class="unreadable-note">This check didn&rsquo;t use one of your free reports.</p>' : '') +
  '</div>';
}

// S1 (2026-10-24): with a hand-review inbox configured (data.hand_review),
// an unreadable file isn't a dead end. "Send it, we'll check it by hand."
// is Phanel-approved wording, kept word for word. The file only leaves the
// browser again if the visitor presses the button, and the note says that
// copy isn't auto-deleted. No reply-time promise for a single file (D4).
function buildHandReview(data, formName) {
  let prefill = '';
  try { prefill = (emailOptinInput && emailOptinInput.value) || localStorage.getItem('tc_email_hint') || ''; } catch (e) {}
  return '<div class="unreadable-box" id="handReviewBox">' +
    '<p class="unreadable-title">Send it, we&rsquo;ll check it by hand.</p>' +
    '<p>We couldn&rsquo;t read the form fields in this file. It may be a flattened e-signed or scanned copy, or it may not be a ' + formName + '. That isn&rsquo;t a finding about your contract.</p>' +
    '<p>Leave your email and we&rsquo;ll send this file (and any addenda you added) to Phanel at TxtAnOffer, who will check it by hand and write back. A hand check looks for the same kinds of blanks and mismatches TC Check flags. It isn&rsquo;t legal advice. A file you send this way is emailed to our inbox and stays there; it isn&rsquo;t deleted automatically the way TC Check&rsquo;s own check is.</p>' +
    '<div class="gate-form"><input type="email" id="handReviewEmail" placeholder="work email" autocomplete="email" value="' + escapeHtml(prefill) + '" onkeydown="if(event.key===\\'Enter\\')sendHandReview()"><button type="button" class="pill solid" id="handReviewBtn" onclick="sendHandReview()">Send it for a hand check</button></div>' +
    '<div class="gate-error" id="handReviewError"></div>' +
    (data.free_report_not_used ? '<p class="unreadable-note">This check didn&rsquo;t use one of your free reports.</p>' : '') +
  '</div>';
}

function sendHandReview() {
  const input = document.getElementById('handReviewEmail');
  const errorEl = document.getElementById('handReviewError');
  const btn = document.getElementById('handReviewBtn');
  const email = (input && input.value || '').trim();
  if (!email || email.indexOf('@') === -1) { errorEl.textContent = 'Enter your email so Phanel can write back.'; return; }
  if (!lastCheckedFiles.length) { errorEl.textContent = 'Drop the file again, then send it.'; return; }
  errorEl.textContent = '';
  btn.disabled = true;
  btn.textContent = 'Sending\u2026';
  const fd = new FormData();
  lastCheckedFiles.forEach(f => fd.append('file', f));
  fd.append('email', email);
  fd.append('source_page', 'tc_check_page');
  try { localStorage.setItem('tc_email_hint', email); } catch (e) {}
  fetch('/v1/tc/check/hand-review', { method: 'POST', body: fd })
    .then(r => r.json())
    .then(d => {
      if (d.error) throw new Error(d.error);
      document.getElementById('handReviewBox').innerHTML =
        '<p class="unreadable-title">Sent.</p>' +
        '<p>Phanel will check it by hand and write back to ' + escapeHtml(email) + '.</p>';
    })
    .catch(err => {
      btn.disabled = false;
      btn.textContent = 'Send it for a hand check';
      errorEl.textContent = (err && err.message) || 'That didn\\'t send. Try again in a minute.';
    });
}

// A TREC form that was read but isn't checked (a 9-18, 25-17, or an
// outdated 20-18 -- tc_audit._form_gate): the one message, in the same
// neutral grey box as an unreadable file. No count, no hand-check offer.
function buildNotChecked(data) {
  const msg = (data.issues && data.issues[0] && data.issues[0].message) || '';
  return '<div class="unreadable-box">' +
    '<p class="unreadable-title">' + escapeHtml(msg.replace(/ -- /g, ' \u2014 ')) + '</p>' +
    '<p>TC Check checks __CHECKED_CONTRACTS__ contracts today.</p>' +
    (data.free_report_not_used ? '<p class="unreadable-note">This check didn&rsquo;t use one of your free reports.</p>' : '') +
  '</div>';
}

function renderResult(data, file, isDemo) {
  checkWrap.classList.add('has-result');
  if (data.unsupported) {
    resultEl.innerHTML = buildMetaBar(data, file) + buildNotChecked(data) + buildNextStepCta();
    resultEl.classList.add('show');
    resultEl.dataset.issues = '[]';
    return;
  }
  if (!data.recognized) {
    resultEl.innerHTML = buildMetaBar(data, file) + buildUnreadable(data, stateMode() === 'co' ? 'CBS1' : 'TREC 20-19') + buildNextStepCta();
    resultEl.classList.add('show');
    resultEl.dataset.issues = '[]';
    return;
  }
  const issues = (data.display_issues && data.display_issues.length) ? data.display_issues : (data.issues || []);
  const totalIssues = typeof data.issue_count === 'number' ? data.issue_count : issues.length;
  // Gate count = must-fix lines in the folded report (per-page initials
  // merged), so it matches the "N must fix" shown once unlocked; raw
  // blocker_count only for an older server response.
  const mustFix = typeof data.must_fix_count === 'number' ? data.must_fix_count
    : (typeof data.blocker_count === 'number' ? data.blocker_count : 0);
  let html = isDemo ? '<div class="demo-banner">Demo result &mdash; a sample contract with planted mistakes, not your file.</div>' : '';
  html += '<div class="rep">' + buildReportHead() + '<div class="rep-body">';
  html += buildReportAddress(data);
  html += buildMetaBar(data, file);

  if (data.complete) {
    html += '<div class="result-banner complete">All checked fields are filled in.</div>';
  } else if (data.gated) {
    // Gated: no counts here -- the gate box below carries the one number.
    const toReview = Math.max(totalIssues - (typeof data.blocker_count === 'number' ? data.blocker_count : 0), 0);
    html += '<div class="result-banner incomplete">' + (mustFix ? 'Not ready &mdash; fix before this goes to title' : toReview + ' item' + (toReview === 1 ? '' : 's') + ' to review') + '</div>';
  } else {
    const nb = issues.filter(i => i.severity === 'blocker').length;
    const nw = issues.length - nb;
    const counts = '<div class="rep-counts">' + (nb ? '<span class="must">' + nb + ' must fix</span>' : '') + (nw ? '<span class="review">' + nw + ' to review</span>' : '') + '</div>';
    html += '<div class="result-banner incomplete">' + (nb ? 'Not ready &mdash; fix before this goes to title' : nw + ' item' + (nw === 1 ? '' : 's') + ' to review') + counts + '</div>';
  }
  if (data.looks_like_blank_draft) {
    html += '<div class="fixit-cta"><p>This looks like an essentially blank draft &mdash; more gaps than a quick fix. It may be faster to generate a clean one from scratch.</p><a href="/demo">Generate a clean offer &rarr;</a></div>';
  }
  if (data.gated) {
    // The server sends no findings while gated; these rows only suggest a
    // list behind the email (count, never location).
    html += '<ul class="issue-list locked" aria-hidden="true">'
      + '<li class="issue-item"><span class="issue-tag blocker">MUST FIX</span><span>&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;</span></li>'
      + '<li class="issue-item"><span class="issue-tag blocker">MUST FIX</span><span>&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;&#9608;</span></li></ul>';
  } else {
    html += buildIssueGroups(issues);
  }
  html += '</div>';
  if (!data.gated && issues.length) {
    html += '<div class="rep-actions">';
    // Not on an unrecognized file: its only finding is about this tool's
    // template match, not anything the agent could fix.
    if (data.recognized) html += '<button class="pill solid" onclick="copyAgentMessage()" data-evt="tc_copy_agent_msg">Copy as message to agent</button>';
    html += '<button class="pill copy-btn" onclick="copyChecklist()">Copy checklist</button>';
    html += '<button class="pill download-btn" onclick="downloadReport(this)">Download report</button>';
    html += '</div>';
  }
  html += '</div>';
  if (data.gated) {
    // trec-qa 2026-10-23: one number (the folded must-fix count) and one
    // button; no category teaser, no shield badge, no stats line.
    const gateHeadline = mustFix > 0
      ? mustFix + ' must-fix item' + (mustFix === 1 ? '' : 's') + ' found.'
      : totalIssues + ' item' + (totalIssues === 1 ? '' : 's') + ' to review found.';
    html += '<div class="gate-box">';
    html += '<p class="gate-headline">' + gateHeadline + '</p>';
    html += '<p class="gate-sub">You&rsquo;ve used your free full reports on this browser. Enter your email to see the exact pages and lines &mdash; your file is deleted after the check.</p>';
    html += '<div class="gate-form"><input type="email" id="gateEmailInput" placeholder="work email" autocomplete="email" onkeydown="if(event.key===\\'Enter\\')unlockGate()"><button type="button" class="pill solid" onclick="unlockGate()">See my full report</button></div>';
    html += '<div class="gate-error" id="gateError"></div>';
    html += '<div class="gate-note">Free, no card, no password &mdash; unsubscribe anytime.</div>';
    html += '</div>';
    html += '<div class="gate-bridge">Leading a team? <a href="/pricing#brokerage">See how Brokerage checks every offer your agents draft here before it&rsquo;s sent &rarr;</a></div>';
  } else {
    if (isDemo) html += buildOwnFileCta(data);
    if (!isDemo && typeof data.free_reports_left === 'number') html += buildEmailReportAsk(data.free_reports_left);
    if (!isDemo) html += buildForwardNextCta() + buildNextStepCta();
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

// Same findings as checklistText(), worded as a note a TC can paste
// straight into an email or text to the agent. Messages go in verbatim --
// no page, paragraph or "title requires" wording beyond what each finding
// already says -- and the name stays "___" because the contract never
// tells us who the agent is. Uses the same folded list as the on-screen
// report (per-page initials already merged by /v1/tc/check).
function agentMessageText() {
  const issues = JSON.parse(resultEl.dataset.issues || '[]');
  if (!issues.length) return '';
  const property = JSON.parse(resultEl.dataset.property || '{}');
  const where = property.address || 'the contract';
  const blockers = issues.filter(i => i.severity === 'blocker');
  const warnings = issues.filter(i => i.severity !== 'blocker');
  const lines = ['Hi ___,', ''];
  if (blockers.length) {
    lines.push('Before this file goes to title, these items need to be fixed on ' + where + ':');
    blockers.forEach((i, n) => lines.push((n + 1) + '. ' + i.message));
  }
  if (warnings.length) {
    // "Also" only reads right after a blocker list; a warnings-only file
    // gets its own lead-in instead.
    if (blockers.length) lines.push('', 'Also worth a look:');
    else lines.push('A few items worth a look on ' + where + ' before this file goes to title:');
    warnings.forEach(i => lines.push('- ' + i.message));
  }
  lines.push('', 'Thanks,');
  return lines.join('\\n');
}

function copyAgentMessage() {
  navigator.clipboard.writeText(agentMessageText());
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
    html = first_screen.fill_tokens(html, src)
    html = (html.replace("__SITE_CHROME_CSS__", _SITE_CHROME_CSS)
                .replace("__SITE_NAV__", _site_nav(cta_href="#check", active="tc"))
                .replace("__SITE_FOOTER__", _SITE_FOOTER)
                .replace("__CHECKED_CONTRACTS__", escape(trec_forms.checked_contracts_line())))
    site_state = _site_state()
    html = _apply_site_state(html.replace("__STATE_SWITCH_JS__", STATE_SWITCH_JS), site_state)
    resp = make_response(html)
    _remember_site_state(resp, site_state)
    if src and not request.cookies.get("ta_src"):
        resp.set_cookie("ta_src", src, max_age=30 * 24 * 3600, httponly=True, samesite="Lax")
        track_event("landing_visit", None, {"source": src})
    track_page_view(resp, "tc_check_page")
    return resp


_BULK_PAGE_STYLE = """
:root{--bg:#F5F5F7;--bg-card:#fff;--border:rgba(15,31,47,0.08);
--text:#0f1f2f;--text-muted:#5a6b7a;--text-dim:#5f707f;--accent:#0b5d52;--accent-light:#16806e;
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
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
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
        if f.get("not_checked"):
            return f'<span class="badge unread">{escape(f["not_checked"])}: not checked</span>'
        if f["unreadable"] or not f["recognized"]:
            return '<span class="badge unread">Couldn&rsquo;t read</span>'
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
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
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
    # Was "Parser Playground" -- type an offer text, see it parsed, generate
    # the PDF via /api/demo. /demo does the same job (plus amend/confirm),
    # and in the footer as "Try a Sample Text" it read to TCs like a sample
    # *check*. One page now; the 301 keeps old links and search results.
    # /api/parse stays for anything still calling it.
    return redirect("/demo", code=301)


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
            "created_at": offer_row.get("created_at"), "generator_version": offer_row.get("generator_version"),
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
        # A signed link proves access to ONE offer, not to whatever source_id
        # the body names -- without this, any valid review link (even your
        # own free-trial one) could point another agent's webhook at your URL
        # and receive their future offers. Bearer-token callers are trusted
        # server-side and keep full control.
        if require_api_auth() is not None:
            owning_offer = get_offer_by_filename(data.get("filename", ""))
            if not owning_offer or owning_offer.get("phone") != source_id:
                return jsonify({"error": "This link can only set the webhook for its own offer."}), 403
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
            "created_at": owning_offer.get("created_at"), "generator_version": owning_offer.get("generator_version"),
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
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
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
    --text-dim: #5f707f;
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

  /* Flat buttons, one line-height for <a> and <button> so all three line
     up (2026-10-13). Yellow = the free check, the primary action; dark
     green = Brokerage; outline = Agent. The old gradient ran to near-black. */
  .cta-btn {
    display:block;width:100%;padding:0.85rem;line-height:1.4;box-sizing:border-box;
    background:var(--accent);color:#fff;
    border:1.5px solid var(--accent);font-family:inherit;font-size:0.9rem;font-weight:600;
    border-radius:var(--radius-sm);cursor:pointer;text-align:center;
    transition:var(--transition);text-decoration:none;
  }
  .cta-btn:hover {transform:translateY(-2px);box-shadow:0 8px 24px rgba(11,93,82,0.25);}
  .cta-btn.primary {background:#f5c242;border-color:#f5c242;color:#0f1f2f;font-weight:700;}
  .cta-btn.primary:hover {background:#ffd25e;border-color:#ffd25e;box-shadow:0 8px 24px rgba(245,194,66,0.35);}
  .cta-btn.dark {background:#0a3f3a;border-color:#0a3f3a;}
  .cta-btn.ghost {background:#fff;border-color:#0a3f3a;color:#0a3f3a;}
  .cta-btn.ghost:hover {background:#0a3f3a;color:#fff;}
  /* Same-height slot under every button, so the Brokerage card's secondary
     link doesn't push its main button above the other two. */
  .plan-sub {min-height:1.4rem;margin-top:0.75rem;text-align:center;font-size:0.85rem;}
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
  <a href="/tc-check" class="nav-cta" data-evt="nav_check_cta">Check a file free</a>
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
    <a href="/tc-check" class="cta-btn primary" data-evt="pricing_check_cta">Check a file free</a>
    <div class="plan-sub" aria-hidden="true"></div>
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
      <button type="submit" class="cta-btn ghost">Subscribe &mdash; $40/mo</button>
    </form>
    <div class="plan-sub" aria-hidden="true"></div>
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
      <li><span class="check">&#10003;</span> Transaction Workspace and roster dashboard</li>
    </ul>
    <a href="/brokers" class="cta-btn dark" data-evt="pricing_audit_cta">Start with a free 20-file audit</a>
    <form action="/create-checkout-session" method="POST" class="plan-sub">
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
      <div class="value-text">No. A file you check is deleted right after your report. If TC Check can&rsquo;t read it and you choose to send it to us for a hand check, that copy stays in our inbox until we delete it. If you get the report by email, its subject line includes the property address, and for bulk checks we keep each file&rsquo;s name and issue count, viewable at your batch link. The other exception is on purpose: on the Brokerage plan, contracts you forward or upload to your archive, and offers your agents text in, are kept 5 years for your records.</div>
    </div>
    <div class="value-card">
      <div class="value-title">Can I cancel?</div>
      <div class="value-text">Yes. Email support@txtanoffer.com and it&rsquo;s cancelled the same day. No contracts. You keep access through the period you paid for.</div>
    </div>
  </div>
</div>

<div class="footer-note">
  By subscribing you agree to our <a href="/terms">Terms of Service</a>.
  <br><br>
  <a href="/tc-check" data-evt="pricing_check_cta">&larr; Try TC File Check</a> &middot; <a href="/">Home</a>
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
    # No signed profile link here. This page used to sign one for the phone
    # typed at Stripe checkout (or for an empty phone when there was no
    # session_id, which any visitor got) -- neither proves the visitor owns
    # that number. The PROFILE keyword texts the link to the phone itself.
    # Security audit 2026-10-23.
    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Welcome to TxtAnOffer!</title>
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
  :root{{--bg:#F5F5F7;--bg-card:#fff;--border:rgba(15,31,47,0.08);
    --text:#0f1f2f;--text-muted:#5a6b7a;--text-dim:#5f707f;
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
        <li><strong>Set up your profile</strong> &mdash; text <strong>PROFILE</strong> to <strong>1-833-897-0333</strong> from the phone you'll draft offers from and we'll text you a private link. Your name, license, and brokerage auto-fill every offer.</li>
        <li>Text your first offer to <strong>1-833-897-0333</strong></li>
        <li>Or use the web demo at <strong>txtanoffer.com/demo</strong></li>
      </ol>
    </div>

    <a href="sms:+18338970333?body=PROFILE" class="btn">Text PROFILE &rarr;</a>
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
    """One screen for one question (trimmed 2026-10-14 at Phanel's ask; it
    had grown to ~11,000px of mostly-zero panels): did a real outside person
    try TC Check, from which channel, and did anyone pay? Headline strip,
    channel table, daily funnel, TC Check senders, revenue, CBS1 tool on top;
    diagnostics collapsed at the bottom; problem panels (send failures, new
    waitlist, brokerage alerts) appear only when they have something in them."""
    if not ANALYTICS_PASSWORD:
        abort(503)
    token = request.args.get("token", "")
    if not hmac.compare_digest(token, ANALYTICS_PASSWORD):
        abort(403)

    from datetime import datetime as _dt, timedelta as _td
    headline = get_headline(days=7)
    revenue = get_revenue_metrics()
    metrics = get_conversion_metrics(days=30)
    daily_funnel = get_daily_funnel(days=14)
    by_source = get_engagement_by_source(days=30)
    tc_check_summary = get_tc_check_summary(days=30)
    tc_check_summary_24h = get_tc_check_summary(days=1)
    tc_repeat_senders = get_tc_check_repeat_senders(within_days=14)
    recent_tc_email_senders = get_recent_tc_check_email_senders(limit=60)
    signups_by_source = get_signups_by_source(days=30)

    def _central(ts):
        try:
            from zoneinfo import ZoneInfo
            return _dt.fromisoformat(ts).replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/Chicago")).strftime("%m-%d %I:%M %p")
        except Exception:
            return ts[5:16]

    def _short(ts):
        return _dt.fromisoformat(ts).strftime("%m/%d %H:%M")

    def _table(headers, rows, empty, ncols=None):
        """Every table sits in its own horizontal scroller so a wide one
        never widens the page on a phone."""
        head = "".join(f"<th>{h}</th>" for h in headers)
        body = rows or f"<tr><td colspan='{ncols or len(headers)}' class='empty'>{empty}</td></tr>"
        return f"<div class='scroll'><table><tr>{head}</tr>{body}</table></div>"

    # --- 2. Real humans by channel -------------------------------------
    def _src_row(r):
        return (f"<tr><td>{escape(r['key'])}</td><td class='dim'>{r['loads']}</td><td><strong>{r['real']}</strong></td>"
                f"<td>{r['stayed']}</td><td>{r['checked']}</td><td>{r['sampled']}</td><td>{r['uploaded']}</td>"
                f"<td class='dim'>{r['scanners']}</td></tr>")
    # Last column (2026-10-19): email security scanners, not in "Real
    # visitors". On a cold-email tag it means the message reached a mail
    # server that opened the link -- delivered, not read.
    src_headers = ["Channel", "<span class='dim'>Page loads (incl. bots)</span>", "Real visitors", "Stayed 10s+",
                   "Clicked &ldquo;Check a file&rdquo;", "Tried sample", "Uploaded",
                   "<span class='dim'>Delivered (scanner hits)</span>"]
    channel_table = _table(src_headers, "".join(_src_row(r) for r in by_source["channels"]), "No page views in the last 30 days.")
    src_table = _table(["src"] + src_headers[1:], "".join(_src_row(r) for r in by_source["sources"]), "No page views in the last 30 days.")

    def _visitor_row(v):
        # js_ok only exists from 2026-10-06; any engagement beacon (drop box
        # seen, 10s stay...) also proves the page's JS ran, so older rows count.
        # Email security scanners (analytics.find_scanners, 2026-10-19) run JS
        # too; they're labeled rather than hidden, since on a cold-email tag
        # they show the message was delivered.
        real = bool(v["events"] - {""}) and not v["scanner"]
        real_cell = (f"scanner <span class='dim'>({escape(v['scanner'])})</span>" if v["scanner"]
                     else '&#10003;' if real else '&mdash;')
        engaged = [label for key, label in (("stay_10s", "10s+"), ("stay_60s", "60s+"), ("dropzone_seen", "saw drop box"),
                                            ("dropzone_interact", "used drop box"), ("upload_picked", "picked a file"),
                                            ("upload_reached_server", "upload reached server"), ("sample_ran", "ran sample"),
                                            ("upload_failed_rejected", "upload rejected"), ("upload_failed_network", "upload failed (network)"),
                                            ("upload_failed_timeout", "upload failed (timeout)"), ("upload_failed_server", "upload failed (server)"))
                   if key in v["events"]]
        # Name the exact button: "clicked check (nav check cta)".
        engaged += ["clicked check (" + e.replace("_", " ") + ")" for e in sorted(v["events"] & set(CHECK_CTA_EVENTS))]
        engaged += ["sample (" + e.replace("_", " ") + ")" for e in sorted(v["events"] & set(SAMPLE_CTA_EVENTS))]
        engaged += [e.replace("_", " ") for e in sorted(v["events"]) if e.endswith("_cta")
                    and e not in CHECK_CTA_EVENTS and e not in SAMPLE_CTA_EVENTS]
        # Escape each page name, then join with a literal &times; -- escaping
        # the joined string turned it into "&amp;times;" (shown as "&times;").
        pages = ", ".join(f"{escape(p)}&times;{n}" if n > 1 else str(escape(p)) for p, n in v["pages"].items()) or "(beacon only)"
        return (f"<tr{'' if real else ' class=dim'}><td>{_central(v['first'])}</td><td class='wrap'>{pages}</td>"
                f"<td>{escape(', '.join(v['referrers']))}</td><td>{escape(', '.join(v['sources']))}</td>"
                f"<td>{escape(', '.join(v['devices']))}</td><td>{real_cell}</td>"
                f"<td class='wrap'>{escape(', '.join(engaged))}</td>"
                f"<td class='ua'>{escape(v['ua'] or '(not logged)')}</td></tr>")
    visitor_headers = ["First seen", "Pages", "Referrer", "Source", "Device", "Real browser", "Did", "Browser (user agent)"]

    # ?src_ua=ti_ lists every visitor whose src starts with that prefix over
    # 30 days, with user agent, so "people or mail scanners?" can be read
    # straight off the UA (the 48h table has rolled past most of them).
    src_ua = re.sub(r"[^a-zA-Z0-9_-]", "", request.args.get("src_ua", ""))[:60]
    src_ua_section = ""
    if src_ua:
        _ua_visitors = get_recent_visitors(hours=30 * 24, limit=300, source_prefix=src_ua)
        src_ua_section = (
            f"<section><h2>Visitors with src starting &ldquo;{escape(src_ua)}&rdquo; (30 days, Central time)</h2>"
            f"<p class='note'>{len(_ua_visitors)} visitors &middot; {sum(1 for v in _ua_visitors if v['events'] - {''} and not v['scanner'])} ran the page "
            f"in a real browser, {sum(1 for v in _ua_visitors if v['scanner'])} marked scanner. Several firms opening in the same minute, no JavaScript, or a Linux/old-Chrome UA = mail scanner, not a person.</p>"
            + _table(visitor_headers, "".join(_visitor_row(v) for v in _ua_visitors), "None.") + "</section>")

    # --- 3. Daily funnel --------------------------------------------------
    daily_rows = "".join(
        f"<tr><td>{d['date'][5:]}</td><td><strong>{d['js_ok']}</strong></td><td>{d['stay_10s']}</td><td>{d['check_cta']}</td>"
        f"<td>{d['dropzone_interact']}</td><td>{d['tried_sample']}</td><td>{d['attempts']}</td>"
        f"<td>{d['email_checks']}</td><td>{d['emails_captured']}</td></tr>"
        for d in daily_funnel)

    # --- 4. TC Check ------------------------------------------------------
    def _sender_row(e):
        if e["dropped"]:
            result = f"<span class='warn'>dropped, no reply ({escape(e['reason'])})</span>"
        elif e["recognized"]:
            result = "recognized"
        else:
            result = "replied, not recognized" + (f" ({escape(e['reason'])})" if e["reason"] else "")
        if e["self"]:
            result += " &middot; <em>your own address</em>"
        return (f"<tr><td>{_short(e['created_at'])}</td><td>{escape(e['sender'])}</td>"
                f"<td>{'known' if e['known_sender'] else 'new'}</td><td>{result}</td></tr>")
    real_senders = [e for e in recent_tc_email_senders if not e["filtered"]][:20]
    filtered_senders = [e for e in recent_tc_email_senders if e["filtered"]]
    sender_headers = ["Time (UTC)", "Sender", "Known?", "Result"]

    # --- 5. Revenue ---------------------------------------------------------
    signup_rows = "".join(f"<tr><td>{escape(s['source'])}</td><td>{s['count']}</td></tr>" for s in signups_by_source)

    # --- 6. CBS1 tool -------------------------------------------------------
    co_tool_rows = "".join(
        f"<tr><td>{escape(r['surface'])}</td><td>{escape(r['host'])}</td><td>{r['runs']}</td><td>{r['ics']}</td></tr>"
        for r in get_co_tool_usage(days=30))

    # --- Only when there's something in them ----------------------------
    alerts = []
    sms_failures = get_recent_sms_failures(limit=20)
    if sms_failures:
        rows = "".join(f"<tr><td>{_short(f['created_at'])}</td><td>{escape(f['phone'] or '')}</td>"
                       f"<td class='wrap'>{escape(f['error'][:80])}</td><td class='wrap'>{escape(f['body'])}</td></tr>" for f in sms_failures)
        alerts.append("<section class='alert'><h2>Recent SMS Send Failures &#9888;</h2>"
                      + _table(["Time (UTC)", "Phone", "Error", "Message"], rows, "") + "</section>")
    email_failures = get_recent_email_send_failures(limit=10, days=7)
    if email_failures:
        rows = "".join(f"<tr><td>{f['time'][5:16].replace('T', ' ')}</td><td>{escape(f['to'])}</td><td class='wrap'>{escape(f['subject'])}</td>"
                       f"<td class='wrap'>{escape(f['error'])}</td></tr>" for f in email_failures)
        alerts.append("<section class='alert'><h2>Outbound Email Failures (7 days) &#9888;</h2>"
                      "<p class='note'>Emails the provider refused (TC Check replies, reports, notifications).</p>"
                      + _table(["Time (UTC)", "To", "Subject", "Error"], rows, "") + "</section>")
    waitlist_signups = get_waitlist_signups(limit=200)
    _wl_cutoff = (_dt.utcnow() - _td(days=30)).isoformat()
    new_waitlist = [w for w in waitlist_signups if w["created_at"] > _wl_cutoff]
    if new_waitlist:
        by_state = {}
        for w in new_waitlist:
            by_state[w["state"]] = by_state.get(w["state"], 0) + 1
        rows = "".join(f"<tr><td>{escape(st)}</td><td>{n}</td></tr>" for st, n in sorted(by_state.items(), key=lambda kv: -kv[1]))
        alerts.append(f"<section><h2>Out-of-State Waitlist: {len(new_waitlist)} new in 30 days ({len(waitlist_signups)} total)</h2>"
                      + _table(["State", "New (30 days)"], rows, "") + "</section>")
    brokerage_alert_delivery = get_brokerage_alert_delivery(days=30)
    if brokerage_alert_delivery["by_source"]:
        rows = "".join(f"<tr><td>{escape(r['source'])}</td><td>{r['alerts']}</td><td>{r['sms_sent']}</td><td>{r['sms_eligible_no_phone']}</td></tr>"
                       for r in brokerage_alert_delivery["by_source"])
        alerts.append(f"<section><h2>Brokerage Alert Delivery (30 days)</h2><p class='note'>{brokerage_alert_delivery['brokerages_with_sms']} of "
                      f"{brokerage_alert_delivery['brokerages_with_sms'] + brokerage_alert_delivery['brokerages_missing_phone']} linked brokerages have a TC phone on file. "
                      "&ldquo;SMS-eligible, no phone&rdquo; would have texted -- add a phone at /admin/brokerages.</p>"
                      + _table(["Source (?src=)", "Alerts sent", "SMS sent", "SMS-eligible, no phone"], rows, "") + "</section>")

    # --- Collapsed diagnostics ---------------------------------------------
    recent_visitors = get_recent_visitors(hours=48)
    recent_real = sum(1 for v in recent_visitors if v["events"] - {""} and not v["scanner"])
    referrer_rows = "".join(f"<tr><td>{escape(r['referrer'])}</td><td>{r['count']}</td></tr>" for r in get_top_referrers(days=30))
    device_rows = "".join(
        f"<tr><td>{d['device']}</td><td><strong>{d['js_ok']}</strong></td><td class='dim'>{d['visitors']}</td><td>{d['stay_10s']}</td>"
        f"<td>{d['dropzone_seen']}</td><td>{d['check_cta']}</td></tr>" for d in get_engagement_by_device(days=7))
    _issues_24h = {i["key"]: i["count"] for i in tc_check_summary_24h["issue_frequency"]}
    issue_rows = "".join(
        f"<tr><td class='wrap'>{escape(i['label'])}</td><td>{i['count']}</td><td>{i['pct_of_recognized']}%</td><td>{_issues_24h.get(i['key'], 0)}</td></tr>"
        for i in tc_check_summary["issue_frequency"])
    sms_rows = "".join(f"<tr><td>{_short(m['created_at'])}</td><td>{escape(m['phone'] or '')}</td><td class='wrap'>{escape(m['body'][:50])}</td></tr>"
                       for m in get_recent_sms(limit=20))
    tc_bulk_summary = get_tc_check_bulk_summary(days=30)
    landing_30 = get_landing_visits_by_source(days=30)
    _landing_24 = {r["source"]: r["count"] for r in get_landing_visits_by_source(days=1)}
    landing_rows = "".join(f"<tr><td>{escape(v['source'])}</td><td>{v['count']}</td><td>{_landing_24.get(v['source'], 0)}</td></tr>" for v in landing_30)

    def _fold(title, body):
        return f"<details class='fold'><summary>{title}</summary>{body}</details>"

    collapsed = "".join([
        _fold(f"Recent Visitors (48 hours, Central time) &middot; {len(recent_visitors)} visitors, {recent_real} real",
              "<p class='note'>One row per browser cookie. Grey rows never ran the page's JavaScript &mdash; almost always link scanners "
              "or preview bots (they don't keep cookies, so each hit is its own row). Your visits are excluded once /internal-mode is set on that device.</p>"
              + _table(visitor_headers, "".join(_visitor_row(v) for v in recent_visitors), "No visitors in the last 48 hours.")),
        _fold("Top Referrers (30 days)",
              _table(["Referring site", "Page views"], referrer_rows, "No page views recorded yet.")
              + "<p class='note'>&ldquo;(none)&rdquo; = typed URL, bookmark, or an app that strips the referrer (LinkedIn's mobile app does).</p>"),
        _fold("Tracking Check: Phone vs Desktop (7 days)",
              _table(["Device", "Real visitors", "<span class='dim'>Page loads (incl. bots)</span>", "Stayed 10s+", "Saw drop box",
                      "Clicked &ldquo;Check a file&rdquo;"], device_rows, "")
              + "<p class='note'>Phones with visitors but ~0 in every other column (while desktop isn't) = a mobile tracking bug, not behavior.</p>"),
        _fold("TC Check: Top Issues (30 days)",
              _table(["Issue", "Count", "% of recognized", "Last 24h"], issue_rows, "No checks recognized yet.")),
        _fold("Recent SMS Activity", _table(["Time (UTC)", "Phone", "Message"], sms_rows, "No SMS yet.")
              + "<p class='note'>Full logs: <a href='https://console.twilio.com/' target='_blank' rel='noopener'>console.twilio.com</a></p>"),
        _fold(f"Bulk TC Check (30 days) &middot; {tc_bulk_summary['batches']} batch{'' if tc_bulk_summary['batches'] == 1 else 'es'}",
              f"<p>{tc_bulk_summary['total_files']} files checked &middot; {tc_bulk_summary['with_issues']} of {tc_bulk_summary['recognized']} "
              f"recognized files had at least one issue ({tc_bulk_summary['with_issues_pct']}%)<br>{tc_bulk_summary['free_batches']} free-tier batches "
              f"(capped at {FREE_BULK_LIMIT} files) &middot; {tc_bulk_summary['brokerage_batches']} with a Brokerage join code (up to {MAX_BULK_FILES})</p>"),
        _fold("Landing Visits by src (30 days, raw clicks)",
              _table(["Source", "30 days", "Last 24h"], landing_rows, "No tagged visits yet.")
              + "<p class='note'>Raw ?src= link opens, scanners included. The channel table above is the people count.</p>"),
    ])

    h = headline
    return f"""<!DOCTYPE html>
<html><head><title>TxtAnOffer Analytics</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
*{{box-sizing:border-box;}}
body{{font-family:system-ui;max-width:920px;margin:0 auto;padding:16px;color:#222;}}
h1{{font-size:22px;margin:8px 0 12px;}}
h2{{font-size:17px;margin:0 0 8px;}}
section{{background:#f5f5f5;padding:14px;margin:12px 0;border-radius:8px;min-width:0;}}
section.alert{{background:#fdecea;}}
.strip{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;}}
.stat{{background:#f5f5f5;border-radius:8px;padding:12px;}}
.stat .v{{font-size:30px;font-weight:700;color:#A9772F;line-height:1.1;}}
.stat .k{{font-size:13px;color:#444;margin-top:4px;}}
.stat .s{{font-size:12px;color:#888;margin-top:2px;}}
.scroll{{overflow-x:auto;-webkit-overflow-scrolling:touch;max-width:100%;}}
table{{border-collapse:collapse;font-size:13px;width:100%;}}
th{{background:#e9e9e9;text-align:left;font-weight:600;white-space:normal !important;min-width:56px;}}
th,td{{padding:5px 8px;white-space:nowrap;vertical-align:top;}}
tr+tr td{{border-top:1px solid #e4e4e4;}}
td.wrap{{white-space:normal;min-width:140px;}}
td.ua{{white-space:normal;font-size:11px;min-width:220px;word-break:break-all;}}
.dim,.dim td{{color:#999;}}
.empty{{color:#888;}}
.warn{{color:#b45309;}}
.note{{color:#666;font-size:12px;margin:8px 0 0;}}
.line{{margin:6px 0;font-size:14px;}}
details.fold{{background:#fafafa;border:1px solid #e4e4e4;border-radius:8px;padding:10px 12px;margin:8px 0;}}
details.fold summary,details summary{{cursor:pointer;font-weight:600;font-size:14px;}}
details.fold[open] summary{{margin-bottom:8px;}}
@media (max-width:600px){{ .strip{{grid-template-columns:1fr 1fr;}} .strip .stat:last-child{{grid-column:span 2;}} .stat .v{{font-size:24px;}} body{{padding:10px;}} }}
</style></head><body>
<h1>TxtAnOffer Analytics</h1>

<div class="strip">
  <div class="stat"><div class="v">{h['real_visitors']}</div><div class="k">Real visitors</div><div class="s">last 7 days</div></div>
  <div class="stat"><div class="v">{h['clicked_check']}</div><div class="k">Clicked &ldquo;Check a file&rdquo;</div><div class="s">any CTA, 7 days</div></div>
  <div class="stat"><div class="v">{h['clicked_book_call']}</div><div class="k">Clicked book a call</div><div class="s">Calendly link, 7 days</div></div>
  <div class="stat"><div class="v">{h['outside_files']}</div><div class="k">Outside files tried</div><div class="s">{h['outside_web']} web &middot; {h['outside_email']} email, 7 days</div></div>
  <div class="stat"><div class="v">${revenue['mrr']:,}</div><div class="k">MRR</div><div class="s">{revenue['active_subscribers']} paying</div></div>
</div>
<p class="note">Real = ran the page in a browser; excludes /internal-mode browsers. Outside files = web uploads from non-internal browsers + email forwards
not from your own addresses or @txtanoffer.com (junk senders and no-PDF emails excluded). Sample runs never count.</p>

<section>
  <h2>Real Humans by Channel (30 days)</h2>
  {channel_table}
  <details style="margin-top:10px;"><summary>By src tag</summary>{src_table}</details>
  <p class="note">Unique browsers, each credited to the src of its first page view in the window. Uploaded = a real web file attempt
  (email forwards aren&rsquo;t tied to a browser). Channels: li_/linkedin &rarr; LinkedIn, fb &rarr; Facebook, ig_ &rarr; Instagram,
  seo_ &rarr; Search, ti_/co_ti_/d1013_/co1012_/*_reach &rarr; Cold email. User agents for one tag: add <code>&amp;src_ua=ti_</code> to this URL.
  <strong>Delivered (scanner hits)</strong> (from 2026-10-19) = email security scanners, never in Real visitors: a made-up browser version,
  a link-protection referrer (Safe Links, Proofpoint, Mimecast, EdgePilot...), or 3+ browsers on one cold-email campaign within 5 minutes.
  Only browsers that did nothing past seeing the drop box can be marked; any click, upload, sample, 10s stay or second page counts as a person.</p>
</section>
{src_ua_section}
<section>
  <h2>Daily Funnel (14 days, UTC)</h2>
  {_table(["Day", "Real visitors", "Stayed 10s+", "Clicked &ldquo;Check a file&rdquo;", "Used drop box", "Tried sample",
           "Uploaded (web)", "Email checks (real senders)", "Emails captured"], daily_rows, "")}
  <details style="margin-top:8px;"><summary style="font-weight:400;font-size:12px;color:#666;">Column definitions</summary>
  <p class="note">Unique browsers except the last three (counts). <strong>Real visitors</strong> ran the page&rsquo;s JavaScript (from 2026-10-06);
  real visitors minus Stayed 10s+ &asymp; bounced. <strong>Clicked &ldquo;Check a file&rdquo;</strong> = any check button or link on any page
  (before 2026-10-14 only a few were tagged, so earlier days undercount). <strong>Used drop box</strong> (from 2026-10-14) = clicked/tapped the
  box, dragged a file over the page, or focused the email field. <strong>Tried sample</strong> = clicked a sample link or ran the sample.
  <strong>Uploaded (web)</strong> = real file attempts, sample excluded. <strong>Email checks</strong> exclude junk, your own addresses and
  no-PDF emails.</p></details>
</section>

<section>
  <h2>TC Check: Recent Email Senders</h2>
  {_table(sender_headers, "".join(_sender_row(e) for e in real_senders), "No forwards from outside senders yet.")}
  <details style="margin-top:8px;"><summary style="font-weight:400;font-size:13px;">Show filtered ({len(filtered_senders)}: junk, automated, own intake, your own address, no PDF)</summary>
  {_table(sender_headers, "".join(_sender_row(e) for e in filtered_senders), "Nothing filtered.")}
  </details>
  <p class="line"><strong>30 days:</strong> {tc_check_summary['total']} files checked ({tc_check_summary['web_count']} web / {tc_check_summary['email_count']} email)
  &middot; {tc_check_summary['recognized']} recognized &middot; {tc_check_summary['complete']} complete</p>
  <p class="line"><strong>Email capture:</strong> {tc_check_summary['emails_captured']} of {tc_check_summary['web_count']} web checks gave an email
  ({tc_check_summary['email_capture_rate']}%); {TC_FREE_FULL_REPORTS} free full reports per browser before the ask</p>
  <p class="line"><strong>Repeat checkers:</strong> {tc_repeat_senders['returned_within_window']} / 10 came back for a 2nd check within
  {tc_repeat_senders['window_days']} days (lifetime; {tc_repeat_senders['distinct_senders']} distinct senders). 10+ = revisit pricing.</p>
</section>

<section>
  <h2>Revenue</h2>
  <p class="line"><strong>{revenue['active_subscribers']}</strong> active subscribers &middot; MRR <strong>${revenue['mrr']:,}</strong> &middot; ARR ${revenue['arr']:,}</p>
  <p class="line">SMS signups (30 days): <strong>{metrics['signups']}</strong> (phones that generated an offer) &rarr; {metrics['conversions']} paid</p>
  {('<details style="margin-top:6px;"><summary style="font-weight:400;font-size:13px;">Signups by source (30 days)</summary>' + _table(["Source", "Signups"], signup_rows, "") + '</details>') if signup_rows else ''}
</section>

<section>
  <h2>CBS1 Deadline Calculator (30 days)</h2>
  {_table(["Where", "Embedding site", "Calculations", ".ics downloads"], co_tool_rows, "No runs yet.")}
  <p class="note">Counts, not people (the widget sets no cookie). &ldquo;page&rdquo; = txtanoffer.com/tools/colorado-deadlines; &ldquo;embed&rdquo; = the widget on another site.</p>
</section>
{"".join(alerts)}
<h2 style="margin-top:20px;">Diagnostics</h2>
{collapsed}
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
        f"<td class='nw' style='padding:8px;color:#5a6b7a;'>{(a['last_offer_at'] or 'never')[:10] if a['last_offer_at'] else 'never'}</td></tr>"
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
            link = "<span style='color:#5f707f;'>expired</span>"
        kind = "Offer (20-19)" if r["kind"] == "offer" else "Amendment (39-11)"
        return (f"<tr><td class='nw' style='padding:8px;'>{(r['created_at'] or '')[:10]}</td>"
                f"<td style='padding:8px;'>{escape(r['address'] or '')}</td>"
                f"<td style='padding:8px;'>{kind}</td>"
                f"<td class='nw' style='padding:8px;'>{escape(r['phone'] or '')}</td>"
                f"<td style='padding:8px;'>{link}</td></tr>")

    record_rows = "".join(_record_row(r) for r in records) or (
        f"<tr><td colspan='5' style='padding:8px;color:#666;'>No records match &ldquo;{escape(archive_q)}&rdquo;.</td></tr>"
        if archive_q else
        "<tr><td colspan='5' style='padding:8px;color:#666;'>No offers drafted by your roster yet.</td></tr>"
    )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>{brokerage['name']} — TxtAnOffer</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
  * {{ box-sizing: border-box; }}
  body{{font-family:'Inter',-apple-system,sans-serif;margin:0;color:#0f1f2f;background:#F5F5F7;-webkit-font-smoothing:antialiased;}}
  .topbar{{background:#0a3f3a;border-bottom:3px solid #f5c242;padding:14px 20px;}}
  .topbar-in{{max-width:900px;margin:0 auto;display:flex;align-items:center;justify-content:space-between;gap:12px;}}
  .topbar img{{height:24px;width:auto;display:block;}}
  .topbar-tag{{font-size:0.72rem;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:#0f1f2f;background:#f5c242;border-radius:999px;padding:4px 10px;white-space:nowrap;}}
  .wrap{{max-width:900px;margin:0 auto;padding:32px 20px 48px;}}
  h1{{font-size:1.6rem;margin:0 0 4px;letter-spacing:-0.02em;}}
  .card h2{{color:#0a3f3a;}}
  a{{color:#0b5d52;font-weight:600;}}
  code{{background:#eef5f3;color:#0a3f3a;padding:1px 6px;border-radius:5px;font-size:0.85em;}}
  td.nw{{white-space:nowrap;}}
  .sub{{color:#5a6b7a;margin-bottom:28px;}}
  .card{{background:#fff;border:1px solid rgba(15,31,47,0.08);border-radius:14px;padding:22px;margin-bottom:24px;}}
  .card h2{{font-size:1.05rem;margin:0 0 14px;}}
  table{{width:100%;border-collapse:collapse;}}
  th{{text-align:left;padding:8px;border-bottom:2px solid #eee;font-size:0.75rem;color:#5f707f;text-transform:uppercase;letter-spacing:0.04em;}}
  td{{border-bottom:1px solid #eee;font-size:0.9rem;}}
  .stat-row{{display:flex;gap:24px;margin-bottom:24px;}}
  .stat{{background:#fff;border:1px solid rgba(15,31,47,0.08);border-top:4px solid #f5c242;border-radius:14px;padding:18px 22px;flex:1;min-width:0;}}
  .stat-num{{font-size:1.7rem;font-weight:800;color:#0b5d52;}}
  .stat-label{{font-size:0.8rem;color:#5f707f;margin-top:2px;}}
  @media(max-width:600px){{
    .wrap{{padding:24px 16px 40px;}}
    .stat-row{{gap:10px;}}
    .stat{{padding:14px 12px;}}
    .stat-num{{font-size:1.35rem;}}
    .stat-label{{font-size:0.72rem;}}
    td{{font-size:0.78rem;}}
    th{{font-size:0.64rem;letter-spacing:0.02em;}}
    th,td{{padding:6px 4px !important;}}
    .card{{padding:18px 12px;}}
  }}
</style>
</head><body>
<header class="topbar"><div class="topbar-in">
  <a href="/"><img src="/static/logo-wordmark-white.png?v=1" alt="TxtAnOffer"></a>
  <span class="topbar-tag">Broker</span>
</div></header>
<div class="wrap">
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
            zf.writestr(f"{(r['created_at'] or '')[:10]}_{name}", filecrypt.read_file(path))
    buf.seek(0)
    track_event("brokerage_archive_export", None, {"brokerage_id": brokerage["id"], "files": len(seen)})
    safe_name = re.sub(r"[^A-Za-z0-9]+", "-", brokerage["name"]).strip("-") or "brokerage"
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{safe_name}-records.zip"'})


# /signup's SMS checkbox label. /privacy's "Opt-in Method" quotes it word for
# word (toll-free reviewers compare the two; tests.test_nurture checks it).
SMS_CONSENT_LABEL = (
    "I agree to receive automated SMS messages from TxtAnOffer at +1 (833) 897-0333 about my offer drafts and account, "
    "including deadline reminders and up to 2 tips during the free trial. No marketing texts. "
    "Reply STOP to opt out, HELP for help. Msg frequency varies. Msg &amp; data rates may apply. Consent is not a condition of purchase or service."
)


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
                # The SMS box is "(Optional)", so it has to mean something: no
                # box, no welcome text (trec-qa 10-23 found it was never read).
                # The exact label shown is stored with the consent.
                # The signed profile link goes only to the phone itself, never
                # into this page: nothing here proves the visitor owns the
                # number they typed, and the same signature opens /dashboard
                # (every offer + PDF link). It used to be a button on this
                # page, so typing any agent's public number handed over their
                # account. Security audit 2026-10-23. Same reply whether or
                # not the phone already had an account.
                sms_sent = False
                if request.form.get("sms_consent"):
                    nurture.record_sms_consent(phone, SMS_CONSENT_LABEL, request.remote_addr)
                    profile_url = sign_dashboard_url(phone, request.host_url.rstrip("/")).replace("/dashboard?", "/profile?")
                    sms_sent = twilio_send_sms(phone,
                        "Welcome to TxtAnOffer! Text your offer: 725k 3% 21day 123 Main St. "
                        f"Set up your profile (auto-fills every offer): {profile_url} "
                        "Msg freq varies. Msg & data rates may apply. Reply HELP for help, STOP to opt out."
                    )
                intro_line = (
                    "Check your texts for a welcome message with your private profile link."
                    if sms_sent else
                    "To set up your profile, text PROFILE to (833) 897-0333 from this phone and we'll text you a private link."
                )
                success_msg = (
                    '<div class="success">'
                    f'<strong>You\'re in!</strong> {intro_line}<br><br>'
                    '<span style="font-size:0.8rem;color:var(--text-muted);">You have 3 free offers to try it out.</span>'
                    '</div>'
                    '<div style="display:flex;gap:0.5rem;margin-top:1rem;flex-wrap:wrap;">'
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
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
  :root{{--bg:#F5F5F7;--bg-card:#fff;--border:rgba(15,31,47,0.08);
    --text:#0f1f2f;--text-muted:#5a6b7a;--text-dim:#5f707f;
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
          <label for="sms-consent">(Optional) {SMS_CONSENT_LABEL} <a href="/privacy">Privacy Policy</a> &amp; <a href="/terms">Terms</a></label>
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
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
  :root{{--bg:#F5F5F7;--bg-card:#fff;--border:rgba(15,31,47,0.08);
    --text:#0f1f2f;--text-muted:#5a6b7a;--text-dim:#5f707f;
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
  <p class="alt">Managing broker? <a href="/broker/login">Sign in to your brokerage dashboard &amp; archive</a></p>
</div>
</body>
</html>"""


# Address shown on the website only (Phanel 2026-10-13: fine on the site,
# not inside emails). TODO(Phanel): legal entity still unconfirmed.
@app.route("/terms")
def terms():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Terms of Service — TxtAnOffer</title>
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
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
    --text-dim: #5f707f;
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
  <a href="/tc-check" class="nav-cta" data-evt="nav_check_cta">Check a file free</a>
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
    <p>These Terms of Service ("Terms") govern your use of TxtAnOffer ("Service"), operated by Phanel Jean-Baptiste, 807 W 112th St, Los Angeles, CA 90044 ("we," "us," or "our"). By accessing or using the Service, you agree to be bound by these Terms. If you do not agree, do not use the Service.</p>

    <h2><span class="section-num">1.</span> Service Description</h2>
    <p>TxtAnOffer is a document drafting and checking tool. For Texas properties it converts shorthand offer text into pre-filled TREC One to Four Family Residential Contract (Resale) forms (TREC No. 20-19); for Colorado properties it fills the Colorado Real Estate Commission's Contract to Buy and Sell Real Estate (Residential) (CBS1) and related Commission forms. The Service accepts offer parameters via SMS or a web interface and generates a partially completed PDF for review by a real estate licensee in the state where the property is located.</p>
    <p>The Service fills in standard TREC and Colorado Real Estate Commission form fields based on information you provide. It does not create custom legal documents, negotiate terms, or exercise professional judgment on your behalf.</p>

    <h2><span class="section-num">2.</span> Not Legal Advice — No Attorney-Client Relationship</h2>
    <div class="emphasis">
      TxtAnOffer is NOT a law firm, does NOT provide legal advice, and does NOT serve as a substitute for consultation with a licensed attorney. No attorney-client relationship is formed by your use of the Service.
    </div>
    <p>The Service performs mechanical form-filling only. It does not:</p>
    <ul>
      <li>Interpret or advise on the legal effect of any contract term</li>
      <li>Evaluate whether a particular offer is appropriate, enforceable, or in your best interest</li>
      <li>Replace the judgment of a qualified real estate attorney</li>
      <li>Provide guidance on TREC or Colorado Real Estate Commission rules, disclosure requirements, or regulatory compliance</li>
    </ul>
    <p>We strongly recommend that all generated documents be reviewed by an attorney licensed in the state where the property is located before execution, particularly for complex transactions, commercial properties, or situations involving material contingencies.</p>

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
      <li>Use of an outdated form version if TREC or the Colorado Real Estate Commission revises a form</li>
      <li>Any downstream consequence of relying on a generated draft without independent review</li>
    </ul>
    <p>THE SERVICE IS PROVIDED "AS IS" AND "AS AVAILABLE" WITHOUT WARRANTIES OF ANY KIND, WHETHER EXPRESS, IMPLIED, STATUTORY, OR OTHERWISE, INCLUDING WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND NON-INFRINGEMENT.</p>

    <h2><span class="section-num">5.</span> TREC and Colorado Real Estate Commission Disclaimer</h2>
    <p>TxtAnOffer is an independent, third-party tool. We are NOT affiliated with, endorsed by, or partnered with the Texas Real Estate Commission (TREC) in any capacity. "TREC" and the form numbers referenced herein are trademarks or designations of the Texas Real Estate Commission.</p>
    <p>We use publicly available TREC promulgated forms as templates. The template currently in use is TREC 20-19, mandatory as of __TREC_FORM_DATE__. If TREC revises or replaces a form, there may be a delay before we update the Service. You are responsible for confirming that the form version used is current and appropriate for your transaction.</p>
    <p>We are likewise NOT affiliated with, endorsed by, or partnered with the Colorado Real Estate Commission or the Colorado Division of Real Estate. Colorado forms are used as published by the Commission for public use; the Service fills in their blanks and does not modify their printed text. The Colorado forms currently supported are the CBS1 (mandatory January 1, 2026) and, for checking or drafting, the Agreement to Amend/Extend Contract, Counterproposal, Inspection Objection Notice, Source of Water Addendum and Exclusive Right-to-Buy Listing Contract. The same responsibility to confirm the current form version applies.</p>

    <h2><span class="section-num">6.</span> Subscription, Payment, and Cancellation</h2>
    <p><strong>Pricing:</strong> Plans start at $40.00 per month, billed monthly via Stripe. See <a href="/pricing" style="color:var(--accent-light);">pricing page</a> for current tiers.</p>
    <p><strong>Billing cycle:</strong> Your subscription renews automatically on the same date each month. You will be charged at the beginning of each billing period.</p>
    <p><strong>Cancellation:</strong> You may cancel your subscription at any time by emailing support@txtanoffer.com. Cancellation takes effect at the end of your current billing period — you retain access until that date.</p>
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
    <p><strong>Data retention:</strong> Generated PDFs on the free trial are stored temporarily and may be deleted after a reasonable period (currently 30 days); on a paid plan they are kept for 5 years from creation and shown in your dashboard. On the Brokerage plan, offers and amendments drafted by an agent linked to the brokerage are kept for 5 years from creation while that agent stays linked, and can be exported at any time from the brokerage dashboard. These are the drafts generated by the Service, not the executed contracts. Brokerage plan accounts may also store executed contracts in the contract archive, by forwarding them from an email linked to the brokerage or uploading them while signed in; those files are kept for 5 years from when they are added unless the brokerage deletes them sooner, are accessible only to the brokerage's signed-in users, and can be exported at any time. You are responsible for having the right to store any document you add. We retain account and billing records as required by law. <strong>Outside the Brokerage plan, this is shorter than the 4-year offer/contract/addenda retention period Texas brokers are independently required to maintain under TREC Rule &sect;535.2, and may be shorter than your own state's broker record-keeping requirements.</strong> Our retention does not satisfy that obligation &mdash; you are responsible for downloading and separately retaining your own copy of every offer and amendment.</p>
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
    <p><strong>Governing law:</strong> These Terms shall be governed by and construed in accordance with the laws of the State of Texas, without regard to its conflict-of-law provisions, except that any dispute arising out of the Service's use for a property located in Colorado, or out of a Colorado Real Estate Commission form generated or checked by the Service, shall be governed by the laws of the State of Colorado, without regard to its conflict-of-law provisions.</p>
    <p><strong>Jurisdiction:</strong> Any legal action or proceeding arising out of or relating to these Terms or the Service shall be brought exclusively in the state or federal courts located in Texas, except that an action governed by Colorado law under the preceding paragraph shall be brought exclusively in the state or federal courts located in Colorado. You consent to the personal jurisdiction of those courts.</p>
    <p><strong>Informal resolution:</strong> Before filing any formal legal proceeding, you agree to attempt to resolve any dispute informally by contacting us. We will attempt to resolve the dispute within 30 days of receiving your notice.</p>

    <h2><span class="section-num">12.</span> Modifications to Terms</h2>
    <p>We reserve the right to modify these Terms at any time. Changes will be effective upon posting to this page with an updated "Last Updated" date. Your continued use of the Service after changes are posted constitutes acceptance of the revised Terms.</p>
    <p>For material changes (including pricing changes), we will provide at least 30 days' notice via email or SMS before the changes take effect.</p>

    <h2><span class="section-num">13.</span> Termination</h2>
    <p>We may suspend or terminate your access to the Service at any time, with or without cause, and with or without notice. Upon termination, your right to use the Service ceases immediately. Sections 2, 3, 4, 7, 8, 9, and 11 survive termination.</p>

    <h2><span class="section-num">14.</span> Contact</h2>
    <p>For questions about these Terms or the Service, contact us at:</p>
    <p>TxtAnOffer<br>Operated by Phanel Jean-Baptiste<br>807 W 112th St, Los Angeles, CA 90044<br>Email: support@txtanoffer.com</p>
  </div>
  <p class="foot">TxtAnOffer is not affiliated with the Texas Real Estate Commission (TREC) or the Colorado Real Estate Commission.<br><a href="/">&larr; Back to home</a> &middot; <a href="/privacy">Privacy Policy</a></p>
</div>
</body>
</html>"""
    return html.replace("__TREC_FORM_DATE__", TREC_FORM_CURRENT_AS_OF)


# Address shown on the website only (Phanel 2026-10-13: fine on the site,
# not inside emails). TODO(Phanel): legal entity still unconfirmed.
@app.route("/privacy")
def privacy():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Privacy Policy — TxtAnOffer</title>
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
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
    --text-dim: #5f707f;
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
  <a href="/tc-check" class="nav-cta" data-evt="nav_check_cta">Check a file free</a>
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
    <span class="updated">Last Updated: October 23, 2026</span>
  </div>

  <div class="legal-card">
    <p>TxtAnOffer ("Service") is operated by Phanel Jean-Baptiste, 807 W 112th St, Los Angeles, CA 90044. This Privacy Policy explains how we collect, use, and protect your information.</p>

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
    <p><strong>Opt-in Method:</strong> Users opt in by (1) entering their phone number and checking an unchecked checkbox on www.txtanoffer.com/signup that says "I agree to receive automated SMS messages from TxtAnOffer at +1 (833) 897-0333 about my offer drafts and account, including deadline reminders and up to 2 tips during the free trial. No marketing texts. Reply STOP to opt out, HELP for help. Msg frequency varies. Msg &amp; data rates may apply. Consent is not a condition of purchase or service." OR (2) by texting offer details directly to +1 (833) 897-0333 after seeing the SMS disclosure on our website.</p>
    <p><strong>Consent:</strong> By texting our service number +1 (833) 897-0333 or checking the SMS box on our signup page, you consent to receive SMS messages from TxtAnOffer about your offer requests and account, including the reminders and tips listed under Message frequency.</p>
    <p><strong>Message frequency:</strong> Message frequency varies based on your usage. You get a reply to each text you send us. We also text you without you texting first, only in these cases:</p>
    <ul>
      <li>A one-time reminder a few days before the closing date of an offer you generated.</li>
      <li>After a listing agent responds through your offer link: a text saying they accepted or declined, and, if accepted, reminders before key contract deadlines (in Texas, the option period end and the earnest money and option fee due date; in Colorado, key &sect;3.1 deadlines).</li>
      <li>On the free trial: up to 2 tips on using the service, about a day after your first and second offers. Each is sent once, between 9am and 8pm Central (noon on Sundays).</li>
      <li>If your brokerage lists you as its transaction coordinator: a text when one of its agents drafts an offer.</li>
    </ul>
    <p><strong>Opt-out:</strong> Reply STOP to any message to unsubscribe from SMS. Reply START to re-subscribe. You can continue using the web interface after opting out of SMS.</p>
    <p><strong>Help:</strong> Reply HELP for support information, or contact support@txtanoffer.com.</p>
    <p><strong>Rates:</strong> Message and data rates may apply depending on your carrier plan.</p>
    <p><strong>Carriers:</strong> Compatible with all major US carriers. Carriers are not liable for delayed or undelivered messages.</p>
    <p>These texts are about offers you generate and your account. Tips explain how to use the service and never advertise a plan or a price. We do not send marketing or promotional messages.</p>

    <h2>4. Data Sharing</h2>
    <p>We do not sell, rent, or trade your personal information. We share data only with:</p>
    <ul>
      <li><strong>Twilio</strong> — SMS delivery (phone number, message content)</li>
      <li><strong>Stripe</strong> — Payment processing (billing details)</li>
      <li><strong>Railway</strong> — Hosting provider; our app server and database run there</li>
      <li><strong>SendGrid</strong> — Inbound email: receives emails (and attached contracts) forwarded to tc@check.txtanoffer.com; also our backup outbound email provider</li>
      <li><strong>DocuSign</strong> — E-signature: when you send an offer for signature, the PDF and the signers&rsquo; names and emails</li>
      <li><strong>Your webhook</strong> — If you set up a webhook (e.g. Zapier), offer data is sent to the URL you choose</li>
      <li><strong>Resend</strong> — Outbound email: sends TC Check report emails (your email address, the property address and the findings), offer emails with the generated PDF, and account notifications; if you send a file for a hand check from the web page, Resend also carries that file to our inbox</li>
      <li><strong>Google (Gmail)</strong> — Hosts the inbox that receives a file you choose to send us for a hand check, with your email address</li>__PRIVACY_GOOGLE__
    </ul>
    <p>We may disclose information if required by law, legal process, or to protect the rights and safety of our users or the public.</p>

    <h2>5. Data Retention</h2>
    <ul>
      <li>Generated PDFs: on the free trial, stored for download and deleted after 30 days; on a paid plan, kept 5 years in your dashboard</li>
      <li>Brokerage plan: offers and amendments drafted by an agent linked to a brokerage are kept for 5 years from creation while the agent stays linked, viewable and exportable by that brokerage</li>
      <li>Brokerage contract archive: executed contracts a brokerage forwards from a linked email or uploads while signed in are stored for 5 years from when they are added (or until the brokerage deletes them), visible only to that brokerage's signed-in users, and exportable anytime. Files checked outside the archive are still deleted after the check (bulk uploads: when the batch finishes), except a file you choose to send us for a hand check.</li>
      <li>TC Check records: for each check we keep a record (your email if you gave one, and short issue codes &mdash; not the file, the property address or the form contents). Records older than 90 days are removed by an automatic cleanup that doesn&rsquo;t run on a fixed schedule, so some may last longer than 90 days. We also keep a per-browser count of free checks (a random browser ID and your email if you gave one) with no set expiry.</li>
      <li>TC Check bulk uploads: we keep your email, the number of files, and each file&rsquo;s original filename, whether it was recognized, its issue count and up to 10 issue messages, with no set expiry. Anyone with the batch results link can view them.</li>
      <li>Hand checks: a file you choose to send us after TC Check couldn&rsquo;t read it is kept in our email inbox with your email, with no set expiry, until we delete it. Ask support@txtanoffer.com to delete it sooner.</li>
      <li>Account data: retained while your account is active and for 90 days after cancellation</li>
      <li>Billing records: retained as required by applicable tax and accounting laws</li>
      <li>SMS logs: kept for support and debugging purposes, deleted after about 90 days</li>
      <li>SMS opt-outs and a record of which follow-up tips we sent: kept with no set expiry, so we never text you after you reply STOP and never send the same tip twice.</li>
    </ul>
    <p><strong>Note for licensees:</strong> In Texas, TREC Rule &sect;535.2 requires brokers to independently retain offers, contracts, and related addenda for at least 4 years from closing or termination. Our 30-day PDF retention does not satisfy that requirement. The Brokerage plan's contract archive can hold the executed contracts your brokerage forwards or uploads, plus the drafts generated here. It helps with that requirement, but it is not a guarantee of compliance &mdash; you remain responsible for keeping complete transaction records under your own state's rules.</p>

    <h2>6. Data Security</h2>
    <p>We implement reasonable technical and organizational measures to protect your data:</p>
    <ul>
      <li><strong>Encryption in transit:</strong> the site is served over HTTPS</li>
      <li><strong>Infrastructure:</strong> hosted on Railway</li>
      <li><strong>Access:</strong> processing is automated, except hand checks you request, which Phanel reads; TxtAnOffer&rsquo;s founder can access stored data for support and debugging</li>
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
    <p>TxtAnOffer<br>Operated by Phanel Jean-Baptiste<br>807 W 112th St, Los Angeles, CA 90044<br>Email: support@txtanoffer.com</p>
  </div>
  <p class="foot">TxtAnOffer is not affiliated with the Texas Real Estate Commission (TREC) or the Colorado Real Estate Commission.<br><a href="/">&larr; Back to home</a> &middot; <a href="/terms">Terms of Service</a></p>
</div>
</body>
</html>"""
    google_line = ("\n      <li><strong>Google (Sign-in)</strong> — Only if a broker chooses \u201cSign in with Google\u201d: Google confirms their email address to us; we never see their Google password, and we use and keep nothing else from their Google account</li>" if google_auth.enabled() else "")
    return html.replace("__PRIVACY_GOOGLE__", google_line)


@app.route("/faq")
def faq():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FAQ — TxtAnOffer</title>
<meta name="description" content="Answers to common questions about TxtAnOffer: TREC affiliation, parser accuracy, data retention, and what the Service does and doesn't cover.">
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
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
    --text-dim: #5f707f;
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
  <a href="/tc-check" class="nav-cta" data-evt="nav_check_cta">Check a file free</a>
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

  <div class="faq-item" id="commands">
    <h2>What can I text TxtAnOffer?</h2>
    <p>Text <strong>(833) 897-0333</strong>. Send your offer terms as <em>price down% days address</em> (e.g. <em>"725k 3% 21day 123 Main St, Austin, TX"</em>), then reply <strong>YES</strong> to create the PDF or <strong>NO</strong> to cancel. Other commands: <strong>DASHBOARD</strong> for a link to your offer history, <strong>STATUS</strong> for your plan and usage, <strong>PROFILE</strong> to edit your agent info, <strong>AMEND</strong> to change a sent offer (below), <strong>HELP</strong> to get this menu by text, and <strong>STOP</strong> to unsubscribe. <a href="/demo#commands" style="color:var(--accent-dark);">See the full command list &rarr;</a></p>
  </div>

  <div class="faq-item">
    <h2>Can I amend an offer after I've already sent it?</h2>
    <p>Yes &mdash; text <strong>AMEND &lt;address&gt; price &lt;value&gt;</strong> or <strong>AMEND &lt;address&gt; close +&lt;days&gt;</strong> (e.g. <em>"AMEND 123 Main St price 730k"</em> or <em>"AMEND 123 Main St close +10"</em>) and you'll get back a filled TREC 39-11 Amendment for that contract. It's included on every plan, works the same way in the <a href="/demo" style="color:var(--accent-dark);">web demo</a>, and shows up nested under the original offer on your <strong>Dashboard</strong>. Only the price or closing-date field you asked to change is filled &mdash; everything else on the form is left blank for you to complete, same as the main contract.</p>
  </div>

  <div class="faq-item">
    <h2>Do you store my texts or offers?</h2>
    <p>On the free trial, generated PDFs are stored for download and deleted after 30 days. On the Agent plan they're kept 5 years in your dashboard. On the Brokerage plan, every offer and amendment your agents draft here, plus any executed contracts your brokerage forwards or uploads, is kept for 5 years in a searchable archive the broker can export. SMS logs are kept for support and debugging and deleted after about 90 days. We do not sell your data. We share it only with the service providers listed in our Privacy Policy, such as Twilio to deliver texts. See our <a href="/privacy" style="color:var(--accent-dark);">Privacy Policy</a> for the full breakdown.</p>
    <p><strong>Important:</strong> TREC Rule &sect;535.2 requires brokers to independently retain records of offers, contracts, and related addenda for at least 4 years from closing or termination of the transaction. Our 30-day free-trial retention does not satisfy that requirement. On the Brokerage plan, the <a href="/archive" style="color:var(--accent-dark);">contract archive</a> holds the executed contracts your brokerage forwards or uploads, kept 5 years, plus the drafts generated here. It helps with that rule, but it isn&rsquo;t a guarantee of compliance &mdash; you remain responsible for keeping complete transaction records.</p>
  </div>

  <div class="faq-item">
    <h2>Can I use this for commercial properties or new construction?</h2>
    <p>TxtAnOffer is designed for residential resale using TREC Form 20-19. For commercial, new construction, or complex transactions, consult a Texas real estate attorney.</p>
  </div>

  <div class="faq-item">
    <h2>Will I get reminded before an offer's closing date?</h2>
    <p>Yes &mdash; TxtAnOffer sends a one-time text a few days before the closing date of an offer you generated, so it doesn't slip past you. It's one of the few texts we send without you texting first. The others are deadline reminders and a notice after a listing agent responds to your offer link, and, on the free trial, up to 2 tips on using the service. Reply STOP anytime to opt out of all messages.</p>
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
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
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
    --text-dim: #5f707f;
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
  <a href="/tc-check" class="nav-cta" data-evt="nav_check_cta">Check a file free</a>
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
    <span class="tag">TREC 36-11</span>
    <h2>Addendum for Property Subject to Mandatory HOA Membership</h2>
    <p>Required whenever the property is subject to mandatory membership in a property owners association -- a common miss when a contract gets typed up quickly. TREC 36-11 replaced 36-10 effective July 1, 2026.</p>
    <div class="handled"><b>How TxtAnOffer handles it:</b> Mention an HOA anywhere in your text and the 36-11 addendum attaches itself automatically, checkbox and all.</div>
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
    # "Common TC Mistakes" only renders once there's real data to show --
    # same 5-file floor as the homepage stats. An empty "No data yet" box
    # was the page's only social signal, and it was a negative one
    # (2026-10-13). Until then the hub points at the sample check instead.
    show_mistakes = bool(mistake_rows) and summary["recognized"] >= 5
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
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
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
    --text-dim: #5f707f;
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
  <a href="/tc-check" class="nav-cta" data-evt="nav_check_cta">Check a file free</a>
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
    <p style="font-size:0.82rem;margin-top:0.5rem;">Working in Colorado? <a href="/tc-hub/colorado" style="color:var(--accent-dark);text-decoration:underline;">Colorado TC Hub &rarr;</a></p>
  </div>

  <div class="hub-nav">
    <a href="#forms">TREC Forms</a>
    <a href="#checklists">File-Review Checklists</a>
    """ + ('<a href="#mistakes">Common TC Mistakes</a>' if show_mistakes else '') + """
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
    <div class="also-covered"><b>Also referenced on this site:</b> IABS 1-2 (Information About Brokerage Services), TREC 61-0 (Seller's Disclosure re: Groundwater/Surface Water Rights), and TREC 36-11 (HOA Addendum) -- see <a href="/trec-changes" style="color:var(--accent-dark);text-decoration:underline;">what changed &rarr;</a> for the full rundown of when each applies.</div>
  </div>

  <div class="section" id="checklists">
    <h2>File-Review Checklists</h2>
    <p class="section-sub">Organized by the point in the transaction where each check actually matters -- built from the exact fields TC Check verifies on a real file, not a generic industry list.</p>
    <p class="section-sub"><a href="/tc-check?demo=1" style="color:var(--accent-dark);font-weight:600;text-decoration:underline;" data-evt="hub_sample_cta">Run the sample to see what TC Check flags &rarr;</a></p>
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

  """ + ("""<div class="section" id="mistakes">
    <h2>Common TC Mistakes</h2>
    <p class="section-sub">What actually shows up blank or inconsistent, ranked by how often TC Check has found it on real uploaded files.</p>
    <div class="mistakes-card">
      """ + mistake_rows + """
      <p class="mistakes-note">""" + mistakes_note + """</p>
    </div>
  </div>""" if show_mistakes else "") + """

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
        <a class="cta-secondary" href="/tc-check" data-evt="hub_check_cta">Want the computer to check it instead? Try TC Check &rarr;</a>
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

  <p class="foot">Questions? Email <a href="mailto:support@txtanoffer.com">support@txtanoffer.com</a>, or <a href="https://calendly.com/pejeanbaptiste/15-min-file-check?utm_source=txtanoffer&amp;utm_content=tc_hub" target="_blank" rel="noopener" data-evt="book_call_hub">book a free 15-min file check</a>.<br><a href="/">&larr; Back to home</a> &middot; <a href="/trec-changes">2026 Changes</a> &middot; <a href="/faq">FAQ</a> &middot; <a href="/terms">Terms</a></p>
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


@app.route("/tc-hub/colorado")
def tc_hub_colorado():
    """Colorado TC Hub -- same page shell as /tc-hub (head, styles, nav),
    Colorado body from co_hub.py."""
    import co_hub
    shell = tc_hub()
    head = shell[:shell.index('<div class="container">')]
    head = head.replace("<title>Texas TC Hub — TxtAnOffer</title>", "<title>Colorado TC Hub — TxtAnOffer</title>")
    head = head.replace(
        '<meta name="description" content="Free reference material for Texas transaction coordinators: TREC form guides, file-review checklists, and the most common mistakes found across real audited closing files.">',
        '<meta name="description" content="Free reference material for Colorado transaction coordinators: Colorado Real Estate Commission forms (CBS1, Amend/Extend, Counterproposal, Exclusive Right-to-Buy), deadline-table checklists and the 2026 changes including HB26-1426.">')
    head = head.replace('<a href="/tc-check" class="nav-cta" ', '<a href="/tc-check?state=co" class="nav-cta" ')
    return head + co_hub.body()


# --- Free CBS1 deadline calculator (2026-10-13) ------------------------------
# No upload: dates in, table out, nothing kept. See co_deadline_tool.py for
# where each rule comes from on the CBS1. The embed route is the only page
# on the site other sites may frame (_frame_headers below).

def _co_tool_host(raw: str) -> str:
    return re.sub(r"[^a-z0-9.-]", "", (raw or "").lower())[:60]


@app.route("/tools/colorado-deadlines")
def co_deadlines_tool():
    import co_deadline_tool as cdt
    shell = tc_hub()
    head = shell[:shell.index('<div class="container">')]
    head = head.replace("<title>Texas TC Hub — TxtAnOffer</title>", f"<title>{escape(cdt.PAGE_TITLE)} — TxtAnOffer</title>")
    head = head.replace(
        '<meta name="description" content="Free reference material for Texas transaction coordinators: TREC form guides, file-review checklists, and the most common mistakes found across real audited closing files.">',
        f'<meta name="description" content="{escape(cdt.PAGE_DESCRIPTION)}">')
    head = head.replace('<a href="/tc-check" class="nav-cta" ', '<a href="/tc-check?state=co" class="nav-cta" ')
    head = head.replace("</head>", cdt.PAGE_CSS + "<style>" + cdt.CALC_CSS + "</style>\n</head>", 1)
    resp = make_response(head + cdt.page_body())
    track_page_view(resp, "co_deadlines_page")
    return resp


@app.route("/embed/colorado-deadlines")
def co_deadlines_embed():
    # No track_page_view: it sets the ta_vid cookie, and the widget promises
    # none. Usage is counted by the anonymous tool_calc_run event instead.
    import co_deadline_tool as cdt
    return cdt.embed_page()


@app.route("/v1/tools/co-deadlines")
def co_deadlines_compute():
    """JSON for the calculator. Stateless: the only write is one anonymous
    tool_calc_run event per page load (log=1), with the surface and the
    embedding site's host -- never the dates entered."""
    import co_deadline_tool as cdt
    result, error = cdt.from_args(request.args)
    if error:
        return jsonify({"error": error}), 400
    if request.args.get("log") == "1":
        surface = "embed" if request.args.get("surface") == "embed" else "page"
        track_event("tool_calc_run", None, {"surface": surface, "host": _co_tool_host(request.args.get("host", "")),
                                            "dated": result["dated"]})
    resp = jsonify(result)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/tools/colorado-deadlines.ics")
def co_deadlines_ics():
    import co_deadline_tool as cdt
    result, error = cdt.from_args(request.args)
    if error:
        return Response(error, status=400, mimetype="text/plain")
    surface = "embed" if request.args.get("surface") == "embed" else "page"
    track_event("tool_ics", None, {"surface": surface, "host": _co_tool_host(request.args.get("host", "")),
                                   "dated": result["dated"]})
    return Response(cdt.to_ics(result), mimetype="text/calendar",
                    headers={"Content-Disposition": 'attachment; filename="cbs1-deadlines.ics"',
                             "Cache-Control": "no-store"})


@app.route("/text-an-offer")
def text_an_offer():
    """How texting an offer works, in depth (text_offer_page.py) -- the
    homepage Text an offer section's "Learn more". Same variables, nav and
    footer as the homepage; the nav's "Text an offer" still goes to /#text,
    where the demo plays."""
    import text_offer_page as top
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Text an offer: how it works — TxtAnOffer</title>
  <meta name="description" content="Text the price, down payment, closing days and address to (833) 897-0333. Reply YES and get a TREC 20-19 draft with those terms filled in, plus the 40-11 when there's a loan. What it reads, what it fills, what stays yours.">
  <link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
  <style>
  /* Homepage variables and type scale (see index()). */
  :root{
    --green:#0b5d52; --green-deep:#0a3f3a; --ink:#1d1d1f; --mute:#6e6e73;
    --gray:#f5f5f7; --line:#e3e3e6; --red:#c62828; --amber:#9a6200;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html{-webkit-font-smoothing:antialiased;scroll-behavior:smooth}
  body{font-family:Inter,-apple-system,system-ui,sans-serif;color:var(--ink);background:#fff;line-height:1.4}
  a{color:inherit;text-decoration:none}
""" + _SITE_CHROME_CSS + """
  section{text-align:center;overflow:hidden}
  .eyebrow{font-size:19px;font-weight:600;color:var(--green);margin-bottom:10px}
  h1{font-weight:800;letter-spacing:-.035em}
  h2{font-size:56px;line-height:1.07;letter-spacing:-.03em;font-weight:800}
  .sub{font-size:24px;line-height:1.35;color:var(--mute);font-weight:500;max-width:680px;margin:18px auto 0}
  .ctas{display:flex;gap:14px;justify-content:center;flex-wrap:wrap;margin-top:30px}
  .btn{display:inline-block;font-size:17px;font-weight:600;padding:13px 26px;border-radius:980px;border:1.5px solid var(--green)}
  .btn.solid{background:var(--green);color:#fff}
  .btn.ghost{color:var(--green)}
  .fine{font-size:13px;color:var(--mute);margin-top:18px}
  @media (max-width:734px){
    .eyebrow{font-size:17px}
    .sub{font-size:19px}
    .btn{font-size:16px;padding:12px 22px}
  }
""" + top.CSS + """  </style>
</head>
<body>
""" + _site_nav() + top.body() + _SITE_FOOTER + """</body>
</html>
"""


@app.route("/trust")
def trust_center():
    """Trust Center -- same page shell as /privacy (head, styles, nav), body
    from trust_center.py."""
    import trust_center as tc
    shell = privacy()
    head = shell[:shell.index('<div class="container">')]
    head = head.replace("<title>Privacy Policy — TxtAnOffer</title>",
                        '<title>Trust Center — TxtAnOffer</title>\n<meta name="description" content="How TxtAnOffer handles your contracts and data: files checked then deleted, rule-based checks with no AI reading your contract, who we share data with, security, and how to report a problem.">')
    head = head.replace("</head>", tc.EXTRA_CSS + "</head>", 1)
    return head + tc.body()


@app.route("/about")
def about():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>About — TxtAnOffer</title>
<meta name="description" content="TxtAnOffer started as a faster way to text in an offer. It became TC File Check &mdash; a free tool that catches what's missing in a TREC 20-19 before title does. Here's the story.">
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
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
    --text-dim: #5f707f;
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
  h1 {font-size:2.1rem;font-weight:800;letter-spacing:-0.03em;line-height:1.12;margin-bottom:0.4rem;color:var(--text);}
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
  <a href="/tc-check" class="nav-cta" data-evt="nav_check_cta">Check a file free</a>
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
  <div class="avatar-lg"><img src="/static/logo.png?v=2" alt="TxtAnOffer"></div>
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
        "description": "A field-by-field checklist for the TREC 20-19 One to Four Family Residential Contract: the blanks and mismatches to check before a Texas file goes to title.",
        "kicker": "For transaction coordinators",
        "h1": "TREC 20-19 checklist: what to check before you send the file to title",
        "lede": "Title problems like these usually aren't legal puzzles. They're blanks and mismatches that were easy to fix before the file left your desk. Here's the order to check them in.",
        "body": """
<p class="note"><strong>Need the blank form?</strong> Get the current TREC 20-19 from the Commission itself: <a href="https://www.trec.texas.gov/forms/one-four-family-residential-contract-resale" target="_blank" rel="noopener" data-evt="guide_trec_official">official TREC 20-19 page &rarr;</a> TxtAnOffer doesn't host or modify the form. <strong>Already filled one in?</strong> <a href="/tc-check?demo=1&amp;src=seo_trec-20-19-checklist" data-evt="guide_sample_cta">See a sample report</a> first, or check your own file with the button at the end of this page.</p>

<h2>1. Property and parties (Sections 1 and 2A)</h2>
<ul>
  <li><strong>Property address, city and county (Section 2A).</strong> Don't skip County; title needs it to open the file.</li>
  <li><strong>Buyer and seller legal names (Section 1).</strong> Match them to how the parties will sign at closing, not a nickname or a partial name.</li>
</ul>

<h2>2. Money and who holds it (Sections 5A and 6A)</h2>
<ul>
  <li><strong>Escrow agent name (Section 5A).</strong> If it's blank, nobody knows where the earnest money goes.</li>
  <li><strong>Earnest money and option fee amounts (Section 5A).</strong> Fill in both. A blank option fee means no termination option under 5D.</li>
  <li><strong>Title company (Section 6A).</strong> If it isn't the escrow agent named in 5A, confirm that's intentional.</li>
</ul>

<h2>3. Financing: contract vs. 40-11 addendum</h2>
<ul>
  <li><strong>Loan amount.</strong> The third-party financing amount in Section 3B must match the principal amount on the TREC 40-11 Third Party Financing Addendum.</li>
  <li><strong>The financing checkboxes.</strong> If a 40-11 is attached, the Third Party Financing Addendum box should be checked in <em>both</em> Section 3B and the Section 22 addenda list. If it's an all-cash deal with no 40-11, neither box should be checked.</li>
</ul>

<h2>4. Initials on every page</h2>
<p>The 20-19 has an "Initialed for identification by Buyer ___ and Seller ___" line at the bottom of every page from 1 to 9 of 12, and the 40-11 has one on its first page. Each party initials once per page: one buyer means one buyer slot filled, and the second buyer slot correctly stays blank. A single missing initial is easy to miss scrolling through.</p>

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
  <li>40-11 initials: one per party on page 1 (one buyer = one buyer box).</li>
</ol>
<p>If the contract is already signed and the numbers disagree, fixing it usually means getting both parties to sign a correction. Catching it before it goes out is much cheaper. When the sales price changes later, remember the financing amount often needs to change with it.</p>

<p class="note">General information about the forms, not legal or lending advice.</p>
<p><a href="/guides/trec-20-19-checklist">See the full TREC 20-19 pre-title checklist &rarr;</a></p>
""",
    },
    "trec-20-19-initials": {
        "title": "TREC 20-19 Initials: Which Pages Need Buyer and Seller Initials",
        "description": "The TREC 20-19 has an initials line on every page from 1 to 9 of 12, plus one on the 40-11 addendum. Here's where they are and how to check them fast.",
        "kicker": "For transaction coordinators",
        "h1": "TREC 20-19 initials: which pages need them, and how to check fast",
        "lede": "A missing initial is easy to miss when you're scrolling 12 pages, and it's quick to fix.",
        "body": """
<h2>Where the initials lines are</h2>
<p>At the bottom of these pages of the TREC 20-19 (the page numbers are printed in each page's footer as "Page X of 12"):</p>
<ul>
  <li>Pages 1 through 9 of 12, every one of them</li>
  <li>Not pages 10&ndash;12: those carry the signatures, broker information and receipts instead</li>
</ul>
<p>Each line reads "Initialed for identification by Buyer ___ ___ and Seller ___ ___", which gives <strong>four slots per page</strong>: two for buyers and two for sellers. Only the slots for parties actually named in Paragraph 1 need initials, so with one buyer and one seller, two slots stay blank, and that's correct. If a TREC 40-11 Third Party Financing Addendum is attached, it has the same line on its first page.</p>

<h2>Why they get missed</h2>
<ul>
  <li>The line is small and sits in the footer, where eyes skip.</li>
  <li>E-signature tools sometimes place initials on some pages but not others, especially when a template was built on an older version of the form.</li>
  <li>With two buyers, it's common for one buyer to initial and the other to be skipped.</li>
</ul>

<h2>How to check fast</h2>
<ol>
  <li>Go to the footer of pages 1 through 9. Check every page, including 2, 3 and 7.</li>
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
    "transaction-coordinator-software-texas": {
        "title": "Transaction Coordinator Software for Texas: Where a Free TREC File Check Fits",
        "description": "What TC platforms do, what a pre-file check on a TREC 20-19 does, and why you might run both. Listed pricing for three TC platforms, with the date checked.",
        "kicker": "For transaction coordinators",
        "h1": "Transaction coordinator software for Texas, and where a free TREC contract check fits",
        "lede": "A blank option period, a 3B loan amount that doesn't match the 40-11, a missing set of initials on page 6. None of those is a task-tracking problem. Here's how TC platforms and a contract check split the work, and what a free file check adds.",
        "body": """
<p class="note"><strong>Read this first:</strong> TxtAnOffer makes TC Check, the free tool described below, so treat this page as written by an interested party. Competitor details come from each company's own website, checked on October 17, 2026, and are listed with sources at the end. Prices and plans change; check their sites for current pricing.</p>
<p><a href="/tc-check?src=seo_transaction-coordinator-software-texas_top" data-evt="guide_check_cta">Have a filled TREC 20-19? Check it free now &rarr;</a></p>

<h2>Two different jobs: running the file vs. checking the contract</h2>
<p>Most transaction coordinator software is built to run the file. It builds the task list, tracks the dates, stores documents, sends the reminder emails and gives agents and clients a portal. Some platforms also advertise contract reading or compliance features; check each vendor's own description of what those cover. Checking whether the contract itself was filled in completely and consistently before it goes to title is its own job.</p>
<p>That second job is where files get kicked back. On a TREC 20-19, the usual causes are boring:</p>
<ul>
  <li><strong>Blanks.</strong> County in Paragraph 2A. Escrow agent in 5A. The option period days in 5B when an option fee is entered in 5A. No box checked in 7B. The Effective Date on page 10.</li>
  <li><strong>Numbers that don't agree.</strong> Paragraph 3C on the form reads "Sales Price (Sum of A and B)", so the cash portion in 3A plus the financing in 3B has to equal it.</li>
  <li><strong>Documents that don't agree.</strong> Here's a made-up example (not a real file): on a 123 Main St contract, 3B reads $320,000 but the attached 40-11 shows a $304,000 loan. Someone has to find out which one is right. Other examples: the Third Party Financing Addendum box checked in 3B and Paragraph 22 but no 40-11 in the file, or a 39-11 amendment that changes the Sales Price when nobody has checked whether 3B and the loan amount changed with it.</li>
  <li><strong>Missing initials.</strong> The 20-19 has an initials line at the bottom of pages 1 through 9 of 12. One party's slot empty on page 6 is easy to miss when you scroll.</li>
</ul>

<h2>What three TC platforms say they do</h2>
<p>These are workflow platforms; two of them also describe AI that reads your contracts. Here's what each one says about itself, and what it charges as listed on October 17, 2026. All three price differently, so the table is a snapshot, not a ranking.</p>
<table style="width:100%;border-collapse:collapse;font-size:0.9rem;margin-bottom:1.2rem;">
<thead><tr style="text-align:left;border-bottom:2px solid rgba(15,31,47,0.15);"><th style="padding:0.5rem 0.6rem 0.5rem 0;">Tool</th><th style="padding:0.5rem 0.6rem;">How it describes itself</th><th style="padding:0.5rem 0;">Pricing as listed Oct 17, 2026</th></tr></thead>
<tbody>
<tr style="border-bottom:1px solid rgba(15,31,47,0.08);vertical-align:top;"><td style="padding:0.6rem 0.6rem 0.6rem 0;"><strong>DocJacket</strong></td><td style="padding:0.6rem;">"Real estate transaction management software," "Built for Transaction Coordinators." "AI prepares the Key Dates, tasks, documents, and messages. You approve." Intake forms, client and agent portals; its Brokerage plan lists compliance and agent submission workflows.</td><td style="padding:0.6rem 0;">Free plan: $0, 2 active transactions. Pro: $29/mo early-customer pricing for 2 coordinator seats, extra seats $29/mo each (page says standard pricing will be $49/mo). Brokerage: starts at $99/mo for up to 25 new transaction files a month, priced by new files opened per month.</td></tr>
<tr style="border-bottom:1px solid rgba(15,31,47,0.08);vertical-align:top;"><td style="padding:0.6rem 0.6rem 0.6rem 0;"><strong>ListedKit</strong></td><td style="padding:0.6rem;">"AI Transaction Management for Real Estate Teams." Its assistant reads contracts and emails, builds timelines, tracks deadlines and, per its pricing page, monitors compliance. Says it reads "any state's purchase agreement."</td><td style="padding:0.6rem 0;">$14.99 per credit, no subscription, first one free. One credit covers one contract setup (a buyer deal is 1 credit; listing to close is 2). Packs bring it down to $11.00 per credit at 50.</td></tr>
<tr style="vertical-align:top;"><td style="padding:0.6rem 0.6rem 0.6rem 0;"><strong>Open To Close</strong></td><td style="padding:0.6rem;">"A fully customizable transaction management platform for Real Estate professionals." Task management, email templates, document tracking, deadlines, agent and client portals.</td><td style="padding:0.6rem 0;">Grow $99/mo, Pro $199/mo, Scale $399/mo; each plan includes 1 user, extra users $69/mo; 30-day free trial.</td></tr>
</tbody></table>
<p>Other platforms exist too (tcDocs, SkySlope, Dotloop, Paperless Pipeline, AFrame, Nekst and more); we haven't listed their details because we haven't verified them. Check each vendor's own site before you decide.</p>

<h2>What a pre-file contract check is</h2>
<p>TC Check is a different kind of tool. You give it a filled-in, fillable TREC PDF and it reads the form fields and tells you what is blank, mismatched or conflicting, citing the paragraph. It runs fixed rules, and no AI reads your contract. It is free: the first 3 reports need no signup, and after that the full itemized report asks for an email. Web uploads are checked and then deleted.</p>
<p>It checks, for example:</p>
<ul>
  <li><strong>TREC 20-19:</strong> blank property address, city, county, escrow agent, earnest money and title company fields (Paragraphs 2A, 5A, 6A); Paragraph 3 price math (3A + 3B against the 3C Sales Price); a blank Effective Date on page 10; a 9A closing date that isn't a real date or falls before the Effective Date; an option fee entered in 5A with the 5B option period days blank; 7B Seller's Disclosure with no box checked; "check one box only" conflicts in 6C, 6E(2), 7B, 7D and 10A; a 12B(1) or 12B(2) broker contribution checked with no amount, or with both the $ and % boxes; the page-12 option fee and earnest money receipts against the 5A amounts; the "Address of Property" header on pages 2 through 12; Paragraph 21 notice email addresses that aren't valid email addresses; and one set of initials for each buyer and seller named in Paragraph 1, on pages 1 through 9.</li>
  <li><strong>TREC 40-11, 41-3 and 39-11:</strong> attach any of these (up to 3 extra files, 4 PDFs in total) and it cross-checks them against the contract. It compares 3B to the 40-11 loan amount, checks the addendum boxes in 3B and Paragraph 22, flags a 39-11 that changes the Sales Price so you can confirm 3B and the 40-11 changed with it, and checks a 41-3's own blanks.</li>
  <li><strong>Colorado CBS1</strong>, plus the Amend/Extend, Counterproposal, Inspection Objection Notice, Source of Water Addendum and Exclusive Right-to-Buy, if you coordinate files there.</li>
</ul>
<p><a href="/tc-check?src=seo_transaction-coordinator-software-texas_body" data-evt="guide_check_cta">Run a filled TREC 20-19 through TC Check &rarr;</a> Or forward the contract email, with the 40-11, 41-3 or 39-11 attached to the same email, to <a href="mailto:tc@check.txtanoffer.com">tc@check.txtanoffer.com</a> and the report comes back by email. Want to see output first? <a href="/tc-check?demo=1&amp;src=seo_transaction-coordinator-software-texas_demo" data-evt="guide_sample_cta">Open the sample report on 123 Main St.</a></p>

<h2>What TC Check does not do</h2>
<ul>
  <li><strong>It is not a TC platform.</strong> No task lists, no deadline tracking for your pipeline, no reminders, no client portal, no document storage for your active files. For dates there's a separate <a href="/guides/trec-deadline-calculator">TREC deadline calculator</a>.</li>
  <li><strong>It needs a fillable PDF.</strong> A scanned or flattened contract, or one from a tool that renamed the form fields, may come back unreadable or unrecognized. Those don't use up a free report.</li>
  <li><strong>It checks the forms listed above.</strong> A TREC form it doesn't have rules for isn't checked, and it says so. It does not read Special Provisions in Paragraph 11 for meaning. It only flags wording that matches a keyword pattern for business terms (dollar amounts, credits, repairs and the like) as something to review. It doesn't check signatures.</li>
  <li><strong>It flags; it doesn't decide.</strong> A clean report means none of its rules fired. It doesn't mean the contract is complete, complies with TREC rules, will close, or is legally sufficient. It is not legal advice. It can't tell you whether the deal terms are right, only whether the specific items listed above are filled in and agree with each other.</li>
  <li><strong>It is not affiliated with TREC.</strong> It doesn't host or change the TREC forms. Get the current forms from <a href="https://www.trec.texas.gov/forms" target="_blank" rel="noopener">TREC's forms page</a>.</li>
</ul>

<h2>How to use both</h2>
<ol>
  <li>When the executed contract and addenda land, run them through TC Check before you key anything into the platform. Send the agent the blanks and mismatches while they're still easy to fix.</li>
  <li>Then load the file into whatever you use to run it: DocJacket, ListedKit, Open To Close, or your own spreadsheet.</li>
  <li>Send the file to title.</li>
</ol>
<p>Why this order: any tool that reads the contract works from what's on the page. If the county is blank or 3B is wrong, getting it corrected first means your timeline starts from the corrected contract.</p>

<h2>Who owns TC Check</h2>
<p>Not owned by Zillow, a brokerage or an MLS. We don't sell data. It's built by one person, who answers the email.</p>

<h2>Sources and dates</h2>
<p class="note">Competitor details are from each company's own site, checked October 17, 2026: <a href="https://www.docjacket.com/" rel="nofollow noopener" target="_blank">docjacket.com</a> and <a href="https://www.docjacket.com/pricing" rel="nofollow noopener" target="_blank">its pricing page</a>; <a href="https://www.listedkit.com/" rel="nofollow noopener" target="_blank">listedkit.com</a> and <a href="https://www.listedkit.com/pricing" rel="nofollow noopener" target="_blank">its pricing page</a>; <a href="https://www.opentoclose.com/" rel="nofollow noopener" target="_blank">opentoclose.com</a> and <a href="https://www.opentoclose.com/v2/pricing" rel="nofollow noopener" target="_blank">its pricing page</a>. Prices are as listed on that date; check their sites for current pricing. Spot an error? Email us and we'll fix it.</p>
""",
    },
}


# Button text per guide: the specific check the article explains. Guides not
# listed keep the generic "Check a file free".
_GUIDE_CTA_TEXT = {
    "trec-20-19-initials": "Check a contract for missing initials",
    "trec-20-19-effective-date": "Check a contract for a blank Effective Date",
    "40-11-loan-amount-mismatch": "Cross-check your 20-19 against its 40-11",
    "trec-39-11-amendment-mismatch": "Check an amendment against its contract",
    "trec-20-19-checklist": "Run this checklist on your file",
}


def _guide_page(slug, g, body_html):
    cta_brokers = g["kicker"] == "For managing brokers"
    cta = (
        """<div class="cta"><strong>See what your agents leave blank.</strong> Run your last 20 closed TREC 20-19s through TC Check, free.
  <a class="btn" href="/brokers?src=seo_""" + slug + """" data-evt="guide_brokers_cta">Free 20-file audit &rarr;</a></div>"""
        if cta_brokers else
        """<div class="cta"><strong>Check a file in seconds.</strong> Drop a filled TREC 20-19 and get back exactly which of these are blank or mismatched. Your first 3 reports need no signup, and the file isn't kept.
  <a class="btn" href="/tc-check?src=seo_""" + slug + """" data-evt="guide_check_cta">""" + _GUIDE_CTA_TEXT.get(slug, "Check a file free") + """ &rarr;</a></div>"""
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
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  :root { --bg:#F5F5F7; --card:#fff; --border:rgba(15,31,47,0.08); --text:#0f1f2f; --muted:#5a6b7a; --dim:#5f707f; --accent:#0b5d52; --accent-light:#16806e; --accent-dark:#0a3a33; --tint:#E7F3F1; }
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
  <a class="nav-cta" href="/tc-check?src=seo_""" + slug + """_nav" data-evt="nav_check_cta">Check a file free</a>
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
:root{--bg:#F5F5F7;--text:#0f1f2f;--muted:#5a6b7a;--dim:#5f707f;--green:#0b5d52;--gd:#0a3f3a;--tint:#E7F3F1;--y:#f5c242;--yt:#FFF6DA;--border:rgba(15,31,47,0.08);}
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
  :root{--bg:#F5F5F7;--text:#0f1f2f;--muted:#5a6b7a;--dim:#5f707f;--green:#0b5d52;--green-dark:#0a3f3a;--tint:#E7F3F1;--yellow:#f5c242;--border:rgba(15,31,47,0.08);}
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
  .gbtn{display:flex;align-items:center;justify-content:center;gap:10px;width:100%;padding:0.7rem 1rem;border:1px solid #dadce0;border-radius:999px;background:#fff;color:#1f1f1f;font-weight:600;font-size:0.95rem;text-decoration:none;}
  .gbtn:hover{background:#f8f9fa;}
  .or{display:flex;align-items:center;gap:10px;margin:1rem 0;color:#8a96a3;font-size:0.8rem;}
  .or:before,.or:after{content:"";flex:1;border-top:1px solid #e6e9ec;}
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
                    f"Sign in to {b['name']}'s archive on TxtAnOffer:\n{link}\n\nThis link expires in 20 minutes. If you didn't ask for it, ignore this email.",
                    f'<div style="font-family:Arial,sans-serif;font-size:14px;color:#0f1f2f;"><p>Sign in to <strong>{escape(b["name"])}</strong>&rsquo;s archive on TxtAnOffer:</p>'
                    f'<p><a href="{escape(link)}" style="display:inline-block;background:#0b5d52;color:#fff;padding:10px 18px;border-radius:999px;text-decoration:none;font-weight:700;">Sign in &rarr;</a></p>'
                    f'<p style="color:#5a6b7a;font-size:12px;">This link expires in 20 minutes. If you didn&rsquo;t ask for it, ignore this email.</p></div>',
                )
                track_event("broker_login_link_sent", None, {"brokerage_id": b["id"]})
        # Same answer either way, so this page can't be used to test which emails are customers.
        msg = '<div class="flash">If that email belongs to a TxtAnOffer brokerage, a sign-in link is on its way. Check your inbox (and spam).</div>'
    body = ("""<div class="card" style="max-width:460px;margin:2rem auto;">
  <h1>Sign in to your archive</h1>
  <p class="sub">""" + ("Sign in with Google, or get a sign-in link by email. No password needed." if google_auth.enabled()
                         else "We&rsquo;ll email you a sign-in link. No password needed.") + """</p>
  """ + msg + """
  """ + _google_button() + """
  <form method="post"><input type="email" name="email" required placeholder="you@brokerage.com" autocomplete="email">
  <button class="btn" style="margin-top:0.8rem;width:100%;">Email me a sign-in link</button></form>
  <p class="muted" style="margin-top:1rem;">Use the email your brokerage signed up with. Not a customer yet? <a href="/brokers">Start with a free 20-file audit</a>.</p>
</div>""")
    return _broker_shell("Sign in", body)


_GOOGLE_G = ('<svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true"><path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9.1 3.6l6.8-6.8C35.8 2.4 30.3 0 24 0 14.6 0 6.6 5.4 2.7 13.2l7.9 6.2C12.5 13.6 17.8 9.5 24 9.5z"/>'
             '<path fill="#4285F4" d="M46.1 24.5c0-1.6-.1-3.1-.4-4.5H24v9h12.4c-.5 2.9-2.2 5.3-4.6 6.9l7.3 5.7c4.3-3.9 7-9.7 7-17.1z"/>'
             '<path fill="#FBBC05" d="M10.6 28.6c-.5-1.4-.8-3-.8-4.6s.3-3.2.8-4.6l-7.9-6.2C1 16.6 0 20.2 0 24s1 7.4 2.7 10.8l7.9-6.2z"/>'
             '<path fill="#34A853" d="M24 48c6.5 0 11.9-2.1 15.9-5.8l-7.3-5.7c-2.2 1.5-5 2.3-8.6 2.3-6.2 0-11.5-4.1-13.4-9.9l-7.9 6.2C6.6 42.6 14.6 48 24 48z"/></svg>')


def _google_button() -> str:
    """Shown only while GOOGLE_CLIENT_ID/SECRET are set (google_auth.enabled)."""
    if not google_auth.enabled():
        return ""
    return ('<a class="gbtn" href="/broker/login/google" data-evt="broker_google_login">' + _GOOGLE_G +
            '<span>Sign in with Google</span></a><div class="or"><span>or</span></div>')


@app.route("/broker/login/google")
def broker_login_google():
    if not google_auth.enabled():
        abort(404)
    url, cookie = google_auth.start()
    resp = make_response(redirect(url))
    resp.set_cookie(google_auth.STATE_COOKIE, cookie, max_age=google_auth.STATE_TTL, httponly=True,
                    secure=request.is_secure or request.headers.get("X-Forwarded-Proto") == "https", samesite="Lax")
    return resp


@app.route("/broker/login/google/callback")
def broker_login_google_callback():
    if not google_auth.enabled():
        abort(404)
    ip = request.remote_addr or "unknown"

    def fail(msg, code=400):
        resp = make_response(_broker_shell("Sign-in failed", '<div class="card" style="max-width:460px;margin:2rem auto;"><h1>Couldn&rsquo;t sign you in</h1><p class="sub">'
                                           + escape(msg) + '</p><a class="btn" href="/broker/login">Back to sign-in</a></div>'), code)
        resp.delete_cookie(google_auth.STATE_COOKIE)
        return resp

    if not check_and_increment(f"broker_google:{ip}", limit=10):
        return fail("Too many attempts. Try again in a few minutes.", 429)
    try:
        email = google_auth.finish(request.args, request.cookies.get(google_auth.STATE_COOKIE, ""))
    except google_auth.GoogleAuthError as e:
        return fail(str(e))
    b = archive_store.find_brokerage_for_login(email)
    if not b or email not in archive_store.login_emails_for(b):
        track_event("broker_google_login_unknown", None, {})
        return fail("That Google account's email isn't on a TxtAnOffer brokerage account. "
                    "Use the email your brokerage signed up with, or ask us to add yours.", 403)
    resp = make_response(redirect("/broker/archive"))
    resp.set_cookie(broker_auth.COOKIE, broker_auth.session_value(b["id"], email), max_age=broker_auth.SESSION_TTL,
                    httponly=True, secure=request.is_secure or request.headers.get("X-Forwarded-Proto") == "https", samesite="Lax")
    resp.delete_cookie(google_auth.STATE_COOKIE)
    track_event("broker_login", None, {"brokerage_id": b["id"], "method": "google"})
    return resp


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
    data = archive_store.read_file(f)
    track_event("archive_download", None, {"brokerage_id": cur[0]["id"], "file_id": file_id})
    safe = re.sub(r"[^A-Za-z0-9._ -]", "_", f["original_name"] or "contract.pdf")
    return Response(data, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{safe}"',
                             "Cache-Control": "no-store"})


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
                zf.writestr(f"executed/{f['created_at'][:10]}_{f['id']}_{safe}", archive_store.read_file(f))
        for r in list_brokerage_records(b["id"], limit=100000):
            path = os.path.join(OUTPUT_DIR, r["filename"])
            if "/" not in r["filename"] and os.path.isfile(path):
                zf.writestr(f"drafts/{(r['created_at'] or '')[:10]}_{r['filename']}", filecrypt.read_file(path))
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
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  :root{--bg:#F5F5F7;--text:#0f1f2f;--muted:#5a6b7a;--dim:#5f707f;--green:#0b5d52;--green-dark:#0a3f3a;--tint:#E7F3F1;--yellow:#f5c242;--border:rgba(15,31,47,0.08);}
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
        return _ARCHIVE_EA_PAGE.replace("__FORM__", '<div class="ok"><b>Thanks &mdash; we&rsquo;ll be in touch shortly</b> to set up your brokerage. In the meantime you can <a href="/tc-check" style="color:#0b5d52;font-weight:600;" data-evt="archive_check_link">check a file free</a>.</div>')
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
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
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
    --text-dim: #5f707f;
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
  .flow {margin:0 0 1rem 1.2rem;color:var(--text-muted);font-size:0.95rem;}
  .flow li {margin-bottom:0.6rem;}
  .flow strong {color:var(--text);}
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
    <a href="/tc-check" data-evt="nav_check_link">TC Check</a>
    <a href="/pricing">Pricing</a>
    <a href="/faq">FAQ</a>
    <a href="/broker/login">Broker Sign-In</a>
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
  <p class="lede">You supervise every file, but you usually only see the broken ones when title kicks them back: <strong>a blank Effective Date, a missing initial, a blank county.</strong> Run your agents' last 20 closed files through TC Check and see the pattern for yourself. Free, no signup, no sales call.</p>

  <div class="offer">
    <h2>Free backlog audit: your last 20 closed files</h2>
    <ol>
      <li><strong>Pull 20 recent closed TREC 20-19 contracts</strong> &mdash; the executed contract PDFs, one per transaction.</li>
      <li><strong>Zip them and upload</strong> &mdash; enter an email so you get a link to the results.</li>
      <li><strong>Get a batch report</strong> &mdash; how many files had at least one issue, and which issues showed up most across your roster.</li>
    </ol>
    <div class="btn-row">
      <a class="btn" href="/tc-check/bulk?src=brokers_page" data-evt="brokers_audit_cta">Start the free audit &rarr;</a>
      <a class="btn-outline" href="https://calendly.com/pejeanbaptiste/15-min-file-check?utm_source=txtanoffer&amp;utm_content=brokers_audit" target="_blank" rel="noopener" data-evt="book_call_brokers">Book a free 15-min file check</a>
    </div>
    <p class="fine">Want to check just one file first? <a href="/tc-check" style="text-decoration:underline;" data-evt="brokers_single_check_cta">Try a single file</a>. TC Check flags what's missing or inconsistent; it isn't legal advice and doesn't replace your own review.</p>
  </div>

  <h2>What the audit looks for</h2>
  <ul class="checks">
    <li><span class="tick">&check;</span><span><strong>Blank required fields</strong> &mdash; Effective Date, earnest money, escrow agent, title fields.</span></li>
    <li><span class="tick">&check;</span><span><strong>Missing initials</strong> &mdash; on every page of the contract.</span></li>
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
      <li>A text the moment it happens: a real blocker on any agent&rsquo;s file texts you directly, before it ever reaches title</li>
    </ul>
    <a class="btn" href="/pricing#brokerage" data-evt="brokers_plan_cta">See the Brokerage plan</a>
  </div>

  <!-- Moved here from the homepage 2026-10-23 (homepage simplification):
       the archive steps and the offer-to-closing workflow, Texas copy. -->
  <h2>Every offer your agents text in, archived for 5 years.</h2>
  <p>TREC asks you to keep transaction records for 4 years from closing (22 TAC &sect;535.2). Here's how files land in your archive &mdash; your agents don't do anything extra.</p>
  <ol class="flow">
    <li><strong>Agents join your roster</strong> &mdash; each agent texts your brokerage's join code before their first offer, or enters it at signup. One time, no per-agent setup.</li>
    <li><strong>They text the offer</strong> &mdash; the TREC 20-19 comes back in seconds, gets checked for blanks and mismatches, and a copy goes to your TC by email.</li>
    <li><strong>Changes are filed with it</strong> &mdash; when an agent texts a price or closing-date change (<code>AMEND 123 Main St price 730k</code>), it becomes a 39-11 amendment, archived under the same address.</li>
    <li><strong>Find it in seconds</strong> &mdash; search by address from your brokerage dashboard, open any PDF, or download the whole archive as a ZIP &mdash; anytime, even if you cancel. <a href="/broker/login" style="color:var(--accent-dark);text-decoration:underline;">Brokerage sign-in &rarr;</a></li>
  </ol>
  <p class="fine">Executed contracts too: forward them to tc@check.txtanoffer.com from your brokerage email, or drop them into your archive &mdash; each one is checked and kept 5 years. <a href="/archive" style="text-decoration:underline;">How the contract archive works &rarr;</a></p>

  <h2>From offer to closing, in one place.</h2>
  <p>TC File Check is the part most people try first. It's one stage of a longer workflow that runs underneath it, start to finish.</p>
  <ol class="flow">
    <li><strong>Offer</strong> &mdash; your agents text the terms in and get back a structured offer in seconds &mdash; the starting point everything else builds on, and the first record in your brokerage's archive. For example: <strong>725k 3% 21day 123 Main St, Austin, TX</strong>. Text <a href="sms:+18338970333" data-evt="workflow_offer_sms" style="color:var(--accent-dark);text-decoration:underline;">(833) 897-0333</a>.
      <span class="fine" style="display:block;margin-top:0.35rem;">By texting, you agree to receive SMS replies and reminders about your offers, plus up to 2 tips on the free trial. Reply STOP to opt out, HELP for help. Msg frequency varies. Msg &amp; data rates may apply. <a href="/privacy" style="color:inherit;text-decoration:underline;">Privacy</a> &middot; <a href="/terms" style="color:inherit;text-decoration:underline;">Terms</a></span></li>
    <li><strong>TREC Documents</strong> &mdash; the required forms are generated straight from the transaction: 20-19, 39-11, 40-11, IABS, 61-0, 36-11.</li>
    <li><strong>Compliance &mdash; TC Check</strong> &mdash; checked for completeness, cross-document consistency, and version-to-version comparison &mdash; before it moves forward.</li>
    <li><strong>Review before it goes out</strong> &mdash; every draft opens on a review page that lists what&rsquo;s still blank. Emailing it to the listing agent stays locked until the required fields are filled.</li>
    <li><strong>Communication</strong> &mdash; SMS confirmations, email notifications, and a listing-agent accept/decline thread, built in from the first text.</li>
    <li><strong>Transaction workspace</strong> &mdash; once accepted, a stage timeline and task checklist track the deal &mdash; option period, earnest money, title, financing, inspections, amendments, final walkthrough &mdash; alongside accept/decline status and dashboard visibility.</li>
    <li><strong>Closing</strong> &mdash; within 5 days of closing, a Closing Checklist pulls it together in one screen: documents complete, signature status, open tasks, and every key date.</li>
  </ol>

  <h2>Who's behind this</h2>
  <p>Built by Phanel Jean-Baptiste, for Texas TCs and brokers. Phanel is a software builder, not a licensed agent. TxtAnOffer is not affiliated with or endorsed by TREC. Questions go straight to me at <a href="mailto:support@txtanoffer.com" style="color:var(--accent-dark);text-decoration:underline;">support@txtanoffer.com</a>. <a href="/about" style="color:var(--accent-dark);text-decoration:underline;">The full story &rarr;</a></p>

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
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
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
    --text-dim: #5f707f;
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
  <a href="/tc-check" class="nav-cta" data-evt="nav_check_cta">Check a file free</a>
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
        # Save only to the phone the signature was checked against.
        # request.values reads the query string before the form, so a
        # signed link for one phone (e.g. the empty-phone link /success
        # used to hand out) plus a different phone in the form body used
        # to verify as the first and save over the second -- anyone could
        # rewrite any agent's name/license/brokerage, which auto-fill into
        # their contracts. Security audit 2026-10-23.
        if request.form.get("phone", "").strip() != phone:
            abort(403)
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
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
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
    --text-dim: #5f707f;
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
  <a href="/tc-check" class="nav-cta" data-evt="nav_check_cta">Check a file free</a>
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
      <input type="text" name="phone" value="{phone}" readonly required>

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


def _js_literal(value):
    """A value as a JS literal, safe inside an inline <script>. The review
    page used to paste the address into '...' by hand, so any address with
    an apostrophe (123 O'Connor St) made the whole script a syntax error:
    Email to Listing Agent, DocuSign and Webhook all silently did nothing.
    json.dumps handles the quoting; "</" is escaped so a value can't close
    the <script> tag early."""
    import json as _json
    return _json.dumps(value).replace("</", "<\\/")


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
    address_html = escape(address or "")
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
        "generator_version": offer.get("generator_version"),
        "financing_type_specified": bool(offer.get("financing_type")),
    }) if os.path.exists(pdf_path_on_disk) else {"ok": False, "blocking": ["PDF file not found on server"], "warnings": []}
    # DocuSign has no "fill in by hand" escape hatch (unlike Email/Download,
    # where the agent can still type the name in before sending) -- a blank
    # buyer/seller legal name must disable this button too, even though
    # it's only a warning for the other two send paths. Matches the
    # server-side check in api_docusign().
    docusign_blocking = validation["blocking"] or [w for w in validation["warnings"] if "legal name is blank" in w]

    pdf_url = f"/offers/{filename}?expires={expires}&sig={sig}"

    # Anchor to the offer's actual creation time, not "now" -- this page can
    # be opened any time after generation, and the closing date is fixed at
    # generation time (baked into the PDF then). Recomputing from "now" here
    # made the summary card silently drift a day later for every day that
    # passes before the agent opens this link, disagreeing with the PDF.
    # deadlines.offer_closing_date is the same function the PDF stamps with.
    close_date = fmt_closing_date(offer_closing_date(close_days, offer.get("created_at"), offer.get("generator_version"))) if close_days else ""
    is_co = filename.startswith("CBS1_")
    if is_co:
        # The CBS1 closing date moves off weekends (co_filler.contract_dates),
        # so show what's actually on the contract.
        from pdf_validator import _read_values
        raw_close = _read_values(pdf_path_on_disk).get("Closing Date", "")
        try:
            cd = datetime.strptime(raw_close, "%m/%d/%Y")
            close_date = f"{cd:%B} {cd.day}, {cd.year}"
        except ValueError:
            pass
    form_meta = ("Colorado CBS1 Contract to Buy and Sell Real Estate (Residential) &middot; current form, mandatory since January 1, 2026"
                 if is_co else f"TREC 20-19 One to Four Family Residential Contract &middot; current form, mandatory since {TREC_FORM_CURRENT_AS_OF}")
    regulator = "the Colorado Real Estate Commission" if is_co else "TREC"

    def _qa_item(msg):
        # Validator messages are plain text written with ASCII "--"; escape
        # them for HTML and give them real dashes for the page.
        return str(escape(msg)).replace(" -- ", " &mdash; ")

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
<title>Offer Review — {address_html}</title>
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
:root{{--bg:#F5F5F7;--bg-card:#fff;--border:rgba(15,31,47,0.08);
--text:#0f1f2f;--text-muted:#5a6b7a;--text-dim:#5f707f;--accent:#0b5d52;--accent-light:#16806e;
--accent-dark:#0a3f3a;--accent-tint:#E7F3F1;--gold:#f5c242;--radius:1.25rem;--radius-sm:0.85rem;}}
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;
-webkit-font-smoothing:antialiased;}}
.site-header{{background:var(--accent-dark);border-bottom:3px solid var(--gold);}}
.site-header-inner{{max-width:600px;margin:0 auto;padding:0.9rem 1rem;display:flex;align-items:center;justify-content:space-between;gap:1rem;}}
.site-header img{{height:22px;width:auto;display:block;}}
.site-header .draft-pill{{background:var(--gold);color:var(--accent-dark);font-size:0.68rem;font-weight:700;
letter-spacing:0.04em;text-transform:uppercase;padding:0.3rem 0.7rem;border-radius:999px;white-space:nowrap;}}
.kicker{{font-size:0.68rem;font-weight:700;color:var(--accent);text-transform:uppercase;letter-spacing:0.07em;margin-bottom:0.35rem;}}
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
@media(max-width:480px){{.site-header .pill-long{{display:none;}}}}
@media(max-width:400px){{
.stats{{grid-template-columns:1fr 1fr;}}
.stat:last-child{{grid-column:span 2;}}
}}
</style>
</head>
<body>
<header class="site-header"><div class="site-header-inner">
<a href="/" aria-label="TxtAnOffer home"><img src="/static/logo-wordmark-white.png?v=1" alt="TxtAnOffer"></a>
<span class="draft-pill">Draft<span class="pill-long"> &middot; review before signing</span></span>
</div></header>
<div class="container">
<div class="address-card">
<div class="kicker">Offer review</div>
<h1>{address_html}</h1>
<div class="meta">{form_meta}</div>
</div>

<div class="stats">
<div class="stat"><div class="stat-label">Price</div><div class="stat-value accent">${price:,}</div></div>
<div class="stat"><div class="stat-label">Down</div><div class="stat-value">{down_pct*100:.0f}% (${down_amt:,})</div></div>
<div class="stat"><div class="stat-label">Close</div><div class="stat-value">{close_date}</div></div>
</div>

{'<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-sm);padding:0.6rem;text-align:center;margin-bottom:1rem;color:var(--text-muted);font-size:0.8rem;">' + ' &middot; '.join([x for x in [f"{mls.get('bed')} Bed" if mls.get('bed') else '', f"{mls.get('bath')} Bath" if mls.get('bath') else '', f"{mls.get('sqft'):,} Sqft" if mls.get('sqft') else '', f"Built {mls.get('year_built')}" if mls.get('year_built') else ''] if x]) + '</div>' if any(mls.get(k) for k in ('bed','bath','sqft')) else ''}

{'<div class="qa-blocking"><strong>Can&rsquo;t send yet &mdash; fix before emailing:</strong><ul>' + ''.join(f'<li>{_qa_item(b)}</li>' for b in validation['blocking']) + '</ul></div>' if validation['blocking'] else ''}
{'<div class="qa-warnings"><strong>Heads up before sending:</strong><ul>' + ''.join(f'<li>{_qa_item(w)}</li>' for w in validation['warnings']) + '</ul></div>' if validation['warnings'] else ''}

{'<div class="sent-banner" id="sent-banner">&#9989; Sent to <strong>' + escape(email_sent_to) + '</strong> on ' + sent_date + '</div>' if email_sent_at else ''}

<div class="pdf-preview-card">
<a href="{pdf_url}" target="_blank"><img src="/offers/{filename}/preview.png?expires={expires}&sig={sig}" alt="Offer PDF preview" class="pdf-preview-img" loading="lazy"></a>
<div class="pdf-preview-caption">Page 1 &middot; <a href="{pdf_url}" target="_blank">View all pages</a></div>
</div>

<div class="actions">
<button class="btn btn-primary" id="email-toggle"{' disabled' if validation['blocking'] else ''}>{'Resend to Listing Agent' if email_sent_at else 'Email to Listing Agent'}</button>
<a href="{pdf_url}" class="btn btn-secondary" target="_blank">Open PDF</a>
<a href="{pdf_url}" class="btn btn-secondary" download="{filename}">Download PDF</a>
</div>

<div class="actions">
<button class="btn btn-secondary" id="docusign-toggle"{' disabled' if docusign_blocking else ''}>Send to DocuSign</button>
<button class="btn btn-secondary" id="webhook-toggle">Webhook / Zapier</button>
</div>

<div class="email-form" id="email-form">
<label>Listing agent's email</label>
<input type="email" id="email-to" placeholder="agent@example.com" value="{escape(email_sent_to)}">
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

<div class="disclaimer">
This is a draft generated by TxtAnOffer. The agent must review every field before signing or presenting.<br>
<a href="/" style="color:var(--accent-dark);">txtanoffer.com</a> &middot; <a href="mailto:support@txtanoffer.com" style="color:var(--accent-dark);">support@txtanoffer.com</a> &middot; Not affiliated with {regulator}
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
    body:JSON.stringify({{to_email:email,pdf_filename:{_js_literal(filename)},parsed:{{address:{_js_literal(address)},price:{price}}},expires:{_js_literal(expires)},sig:{_js_literal(sig)}}})
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
    body:JSON.stringify({{pdf_filename:{_js_literal(filename)},signer_email:email,signer_name:name,parsed:{{address:{_js_literal(address)}}},expires:{_js_literal(expires)},sig:{_js_literal(sig)}}})
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
    body:JSON.stringify({{source_id:{_js_literal(offer['phone'])},url:url,filename:{_js_literal(filename)},expires:{_js_literal(expires)},sig:{_js_literal(sig)}}})
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
                if offer.get("close_days"):
                    close_date = offer_closing_date(offer["close_days"], offer.get("created_at"), offer.get("generator_version"))
                summary_lines = _day_one_lines(
                    filename, offer["address"], effective_date, offer.get("option_days"), close_date
                )
                twilio_send_sms(offer["phone"], "\n".join(summary_lines))
                transaction_tasks.seed_default_tasks(
                    filename, effective_date, offer.get("option_days"), close_date,
                    pdf_path=os.path.join(OUTPUT_DIR, filename)
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
    # the anonymous demo (source_id "demo-web:<token>") or its placeholder values
    # ("Your Name Here" etc), which would look like a fake identity on a
    # page a real listing agent might open, and never for Starter, which
    # doesn't include agent branding.
    agent_card_html = ""
    sending_phone = offer.get("phone") or ""
    if sending_phone and not sending_phone.startswith("demo-web") and has_professional_access(sending_phone):
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
        "generator_version": offer.get("generator_version"),
        "financing_type_specified": bool(offer.get("financing_type")),
    }) if os.path.exists(pdf_path_on_disk) else {"ok": False, "blocking": [], "warnings": []}

    try:
        created_dt = datetime.fromisoformat(offer["created_at"])
    except (KeyError, TypeError, ValueError):
        created_dt = datetime.now()
    close_date = fmt_closing_date(offer_closing_date(close_days, offer.get("created_at"), offer.get("generator_version"))) if close_days else ""

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
        close_date_obj = offer_closing_date(close_days, offer.get("created_at"), offer.get("generator_version")) if close_days else None
        summary_lines = _day_one_lines(filename, address, effective_date, offer.get("option_days"), close_date_obj)
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
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
:root{{--bg:#F5F5F7;--bg-card:#fff;--border:rgba(15,31,47,0.08);
--text:#0f1f2f;--text-muted:#5a6b7a;--text-dim:#5f707f;--accent:#0b5d52;--accent-light:#16806e;
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
            if not label or stage not in transaction_tasks.stages_for(filename):
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
    close_date_obj = offer_closing_date(close_days, offer.get("created_at"), offer.get("generator_version")) if close_days else None

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

    stages = transaction_tasks.stages_for(filename)
    co_values = None
    if transaction_tasks.is_co(filename):
        import co_audit
        co_values = co_audit.read_contract(os.path.join(OUTPUT_DIR, filename))
        if co_values:
            # The CBS1 Closing Date moves off weekends/holidays (co_filler),
            # so use the one printed on the contract.
            close_date_obj = co_audit.contract_key_dates(co_values)["closing"] or close_date_obj
    transaction_tasks.seed_default_tasks(filename, effective_date, offer.get("option_days"), close_date_obj,
                                         pdf_path=os.path.join(OUTPUT_DIR, filename))
    tasks = transaction_tasks.get_tasks(filename)

    tasks_by_stage = {s: [] for s in stages}
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
    for s in stages:
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
    for s in stages:
        st = stage_state[s]
        timeline_html += f'<div class="tl-node tl-{st}"><div class="tl-dot"></div><div class="tl-label">{s}</div></div>'

    def _fmt(d):
        return d.strftime("%b %d, %Y") if d else "—"

    import json as _json

    stage_sections_html = ""
    for s in stages:
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
          <form class="add-task-form" onsubmit="return addTask(event, {escape(_json.dumps(s))})">
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
        if co_values is not None or transaction_tasks.is_co(filename):
            earnest_dl, opt_end = None, None
        else:
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
            {f'<div>Earnest money due: {_fmt(earnest_dl)}</div>' if earnest_dl else ''}
            {f'<div>Option period ends: {fmt_option_end(opt_end)}</div>' if opt_end else ''}
            <div>Closing: {_fmt(close_date_obj)}</div>
          </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Transaction — {address}</title>
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
<link rel="preload" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" as="style" onload="this.onload=null;this.rel='stylesheet'"><noscript><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"></noscript>
<style>
:root{{--bg:#F5F5F7;--bg-card:#fff;--border:rgba(15,31,47,0.08);
--text:#0f1f2f;--text-muted:#5a6b7a;--text-dim:#5f707f;--accent:#0b5d52;--accent-light:#16806e;
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
    path = os.path.join(OUTPUT_DIR, filename)
    if not os.path.isfile(path):
        abort(404)
    return Response(filecrypt.read_file(path), mimetype="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{os.path.basename(filename)}"',
                             "Cache-Control": "private, no-store"})


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
    doc = fitz.open(stream=filecrypt.read_file(pdf_path), filetype="pdf")
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
    # A link for no phone isn't anyone's dashboard/profile -- /success used
    # to sign one for every visitor, which then opened /profile's save form.
    if not phone:
        return False
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
a{color:#0b5d52;font-weight:600;text-decoration:none;}
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
        sub_badge_text = "#0a3f3a"
    else:
        sub_status = f"Free ({user['offer_count']}/{FREE_OFFER_LIMIT} used)"
        sub_badge_color = "#FFF6DA"
        sub_badge_text = "#8a5a00"

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
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
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
    --text-dim: #5f707f; /* was #8a9aa9, under WCAG AA on white */
    --accent: #0b5d52;
    --accent-light: #16806e;
    --accent-dark: #0a3a33;
    --accent-tint: #eef5f3;
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
  .stat-val {{font-size:1.75rem;font-weight:800;color:var(--accent);white-space:nowrap;line-height:1.15;}}
  .stat-label {{font-size:0.7rem;font-weight:600;color:var(--text-dim);margin-top:0.25rem;
    text-transform:uppercase;letter-spacing:0.06em;}}

  .profile-card {{
    background:var(--bg-card);
    border:1px solid var(--border);border-radius:var(--radius);
    padding:1.5rem 1.75rem;margin-top:1rem;
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
  .offer-card-bar.status-draft {{background:#f5c242;}}
  .offer-card-bar.status-sent {{background:linear-gradient(180deg, #0b5d52, #16806e);}}
  .offer-card-bar.status-expired {{background:linear-gradient(180deg, #9ca3af, #cbd5e1);}}
  .offer-card-bar.status-accepted {{background:#0a3f3a;}}
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
  .status-badge.status-draft {{background:#f5c242;color:#0f1f2f;}}
  .status-badge.status-sent {{background:var(--accent-tint);color:#0a3f3a;}}
  .status-badge.status-expired {{background:rgba(148,163,184,0.18);color:#4b5563;}}
  .status-badge.status-accepted {{background:#0a3f3a;color:#fff;}}
  .status-badge.status-declined {{background:rgba(244,63,94,0.12);color:#be123c;}}

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
  .nav-item svg {{width:20px;height:20px;display:block;}}
  .nav-item:hover, .nav-item.active {{color:var(--accent);}}
  .nav-item.active {{position:relative;}}
  .nav-item.active::before {{content:"";position:absolute;top:-0.7rem;left:20%;right:20%;height:3px;border-radius:2px;background:#f5c242;}}

  @media(max-width:600px){{
    .container {{padding:1.5rem 1rem 1rem;}}
    .stats {{grid-template-columns:1fr 1fr;}}
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
    .stats {{gap:0.6rem;}}
    .stat {{padding:1rem 0.85rem;}}
    .stat-val {{font-size:1.3rem;}}
    .stat-label {{font-size:0.62rem;letter-spacing:0.04em;}}
    .nav {{padding:1rem 1.25rem;}}
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
  <a href="/dashboard?phone={_urlquote(phone, safe='')}&expires={expires}&sig={sig}" class="nav-item active"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/></svg>Dashboard</a>
  <a href="{profile_url}" class="nav-item"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-7 8-7s8 3 8 7"/></svg>Profile</a>
  <a href="/pricing" class="nav-item"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20"/></svg>{'Billing' if user['is_subscribed'] else 'Upgrade'}</a>
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
<link rel="icon" href="/static/favicon.ico?v=2" type="image/x-icon">
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
  .logo {{margin:0 auto 1.5rem;display:flex;justify-content:center;}}
  .logo img {{height:26px;width:auto;display:block;}}
  .agent-name {{font-size:0.95rem; font-weight:700; color:#0f1f2f;}}
  .agent-meta {{font-size:0.8rem; color:#5f707f; margin-top:0.15rem; margin-bottom:1.75rem;}}
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
  .foot {{font-size:0.72rem; color:#5f707f; margin-top:1.5rem;}}
  .foot a {{color:#5a6b7a; font-weight:600;}}
</style>
</head>
<body>
  <div class="card">
    <div class="logo"><img src="/static/logo-wordmark.png?v=2" alt="TxtAnOffer"></div>
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
# on purpose and disallowed in robots.txt. /tc-check/compare is live (linked
# from /tc-check) but kept out until anyone uses it; /playground is a 301.
# /embed/colorado-deadlines is an iframe widget, not a page -- kept out too.
_SITEMAP_PATHS = ["/", "/tc-check", "/brokers", "/pricing", "/trec-changes", "/tc-hub", "/tc-hub/colorado", "/trust",
                  "/tc-check/bulk", "/faq", "/about",
                  "/contact", "/privacy", "/terms", "/guides",
                  "/guides/trec-20-19-checklist", "/guides/trec-20-19-effective-date",
                  "/guides/broker-record-retention-texas", "/guides/40-11-loan-amount-mismatch",
                  "/guides/trec-20-19-initials", "/guides/trec-39-11-amendment-mismatch",
                  "/guides/trec-deadline-calculator", "/guides/transaction-coordinator-software-texas", "/archive", "/tools/colorado-deadlines",
                  "/text-an-offer"]


@app.route("/favicon.ico")
def favicon():
    # Browsers and Google's result snippets request /favicon.ico at the root
    # regardless of the <link rel="icon">; serve the same "t" mark from static.
    return send_from_directory(os.path.join(app.root_path, "static"), "favicon.ico",
                               mimetype="image/x-icon", max_age=86400)


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
