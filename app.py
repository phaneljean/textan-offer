"""
app.py — Twilio SMS webhook for TxtAnOffer, plus a /demo web form that
bypasses SMS entirely (for testing while A2P 10DLC registration is pending).

Flow (SMS):
  Agent texts "725k 3% 21day 1740 Grand Ave"
    -> parse_offer_sms() extracts structured data
    -> (stub) pull real bed/bath/sqft from MLS -- replace with real API call
    -> fill_offer_pdf() writes values into 20-19_2.pdf
    -> reply with a summary + link to review/sign

Flow (demo, no SMS/Twilio needed):
  Visit /demo -> type the same offer string into a web form -> same
  parse/fill logic runs -> result + PDF link shown directly on the page.
"""

from flask import Flask, request, send_from_directory, Response, redirect, jsonify, abort
from twilio.twiml.messaging_response import MessagingResponse
from datetime import datetime
import os
import hmac
import hashlib
import time
import stripe

from parser import parse_offer_sms
from pdf_filler import fill_offer_pdf, OUTPUT_DIR
from agent_profiles import get_agent_profile, save_agent_profile
from subscriptions import can_generate_offer, increment_offer_count, activate_subscription, deactivate_subscription, get_user, create_user, FREE_OFFER_LIMIT
from analytics import track_event, get_conversion_metrics, get_revenue_metrics, get_recent_sms
from integrations import send_offer_email, fire_webhook, save_webhook, get_webhook, delete_webhook, send_to_docusign
from offers_db import record_offer, get_offers_for_phone

app = Flask(__name__)

# Stripe configuration
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")
STRIPE_PRICE_ID_PRO = os.environ.get("STRIPE_PRICE_ID_PRO", "")
STRIPE_PRICE_ID_BROKERAGE = os.environ.get("STRIPE_PRICE_ID_BROKERAGE", "")

PDF_LINK_SECRET = os.environ.get("PDF_LINK_SECRET", "change-me-in-production")
PDF_LINK_TTL = int(os.environ.get("PDF_LINK_TTL", 86400))  # 24 hours


def sign_pdf_url(filename, base_url=""):
    expires = int(time.time()) + PDF_LINK_TTL
    sig = hmac.new(PDF_LINK_SECRET.encode(), f"{filename}:{expires}".encode(), hashlib.sha256).hexdigest()[:16]
    return f"{base_url}/offers/{filename}?expires={expires}&sig={sig}"


def verify_pdf_signature(filename, expires_str, sig):
    try:
        expires = int(expires_str)
    except (ValueError, TypeError):
        return False
    if time.time() > expires:
        return False
    expected = hmac.new(PDF_LINK_SECRET.encode(), f"{filename}:{expires}".encode(), hashlib.sha256).hexdigest()[:16]
    return hmac.compare_digest(sig or "", expected)


@app.route("/")
def index():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>TxtAnOffer — Generate TREC Contracts by Text Message</title>
  <meta name="description" content="Texas real estate agents: text your offer details and receive a filled TREC 1-4 contract PDF in under 10 seconds. No app required.">
  <link rel="icon" href="/static/favicon.ico" type="image/x-icon">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
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
      border-radius: 50%;
      overflow: hidden;
    }
    .nav-logo img {
      width: 100%; height: 100%;
      object-fit: cover;
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
    .pdf-preview {
      background: rgba(255,255,255,0.03);
      border: 1px solid rgba(255,255,255,0.06);
      border-radius: var(--radius-sm);
      padding: 0.75rem;
      display: flex;
      align-items: center;
      gap: 0.6rem;
      margin-top: 0.25rem;
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
    .pdf-name { font-size: 0.78rem; color: #e2e8f0; font-weight: 500; }
    .pdf-meta { font-size: 0.68rem; color: #475569; }

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
    .footer-copy { color: #475569; font-size: 0.8rem; }

    @media (max-width: 960px) {
      .hero { grid-template-columns: 1fr; padding: 3rem 1.5rem; gap: 3rem; }
      .hero h1 { font-size: 2.5rem; }
      .phone-wrap { order: -1; }
      .phone { width: 260px; }
      .steps-grid { grid-template-columns: 1fr; }
      .nav-links { display: none; }
      .stats { gap: 1.5rem; }
    }
    @media (max-width: 480px) {
      .hero h1 { font-size: 2rem; }
      .input-row { flex-direction: column; }
      .input-btn { width: 100%; }
      .phone { width: 240px; }
      .nav { padding: 1rem; }
    }
  </style>
</head>
<body>

  <nav class="nav">
    <div class="nav-left">
      <div class="nav-logo"><img src="/static/logo.webp" alt="TxtAnOffer"></div>
      <span>TxtAnOffer</span>
    </div>
    <div class="nav-links">
      <a href="#how">How it works</a>
      <a href="/pricing">Pricing</a>
      <a href="/demo">Demo</a>
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
        Type your offer in plain English. Get a filled <strong>TREC 20-19</strong> + <strong>Third Party Financing Addendum</strong> PDF in under 10 seconds. No app. No login. Just text.
      </p>

      <div class="input-card">
        <div class="input-label">Try it now &mdash; no signup required</div>
        <form id="live-demo-form">
          <div class="input-row">
            <input type="text" id="demo-input" placeholder="725k 3% 21day 1740 Grand Ave, Austin TX 78701" autocomplete="off">
            <button type="submit" class="input-btn">Generate &rarr;</button>
          </div>
        </form>
        <div class="input-hint">Format: price &middot; down % &middot; closing days &middot; address</div>
        <div class="demo-loading" id="demo-loading">Generating your contract...</div>
        <div class="demo-error" id="demo-error"></div>
        <div class="demo-result" id="demo-result">
          <div class="res-row"><span class="k">Address</span><span class="v" id="res-addr"></span></div>
          <div class="res-row"><span class="k">Price</span><span class="v" id="res-price"></span></div>
          <div class="res-row"><span class="k">Down payment</span><span class="v" id="res-down"></span></div>
          <div class="res-row"><span class="k">Closing</span><span class="v" id="res-close"></span></div>
          <a href="#" id="res-pdf" class="res-link" target="_blank">Download PDF &rarr;</a>
        </div>
      </div>

      <div class="stats">
        <div><div class="stat-num">&lt;10s</div><div class="stat-label">Generation time</div></div>
        <div><div class="stat-num">45 min</div><div class="stat-label">Saved per offer</div></div>
        <div><div class="stat-num">Free</div><div class="stat-label">No card required</div></div>
      </div>

      <div class="social-proof">
        <div class="avatars">
          <div class="avatar" style="background:#3b82f6;">EJ</div>
          <div class="avatar" style="background:#8b5cf6;">MK</div>
          <div class="avatar" style="background:#f59e0b;">SR</div>
          <div class="avatar" style="background:var(--accent);">+</div>
        </div>
        <div class="social-text"><strong>200+ Texas agents</strong> already using TxtAnOffer</div>
      </div>
    </div>

    <div class="phone-wrap">
      <div class="phone-glow"></div>
      <div class="phone">
        <div class="phone-notch"></div>
        <div class="phone-screen">
          <div class="msg-time">Today 9:41 AM</div>
          <div class="msg-bubble msg-user">725k 3% 21day 1740 Grand Ave, Austin TX 78701</div>
          <div class="msg-bubble msg-bot">
            Your TREC contract is ready!<br><br>
            <strong style="color:#fff;">$725,000</strong><br>
            Close: <strong style="color:#fff;">Aug 12, 2026</strong><br><br>
            <a>txtanoffer.com/offers/1740-grand-ave.pdf</a>
          </div>
          <div class="pdf-preview">
            <div class="pdf-icon">PDF</div>
            <div>
              <div class="pdf-name">TREC_1740_Grand_Ave.pdf</div>
              <div class="pdf-meta">142 KB &middot; TREC 20-19 + 40-11</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </section>

  <section class="steps" id="how">
    <div class="steps-header">
      <h2>Three steps. No app required.</h2>
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
    </div>
  </section>

  <section class="sms-section">
    <h2>SMS Messaging Details</h2>
    <div class="sms-card">
      <h3>How SMS Is Used</h3>
      <ul>
        <li><strong>Opt-in:</strong> Users sign up at txtanoffer.com/signup by providing their phone number and explicitly consenting to receive SMS messages.</li>
        <li><strong>Message frequency:</strong> Messages are sent only in direct response to user-initiated texts. We do not send marketing or promotional messages.</li>
        <li><strong>Message content:</strong> Replies contain contract confirmation details and a download link to the generated PDF.</li>
        <li><strong>Opt-out:</strong> Reply STOP at any time to unsubscribe from all messages. Reply HELP for support.</li>
        <li><strong>Standard message and data rates may apply.</strong></li>
      </ul>
      <div class="sms-contact">
        Questions? Contact us at <a href="mailto:support@txtanoffer.com">support@txtanoffer.com</a>
      </div>
    </div>
  </section>

  <footer class="footer">
    <div class="footer-links">
      <a href="/terms">Terms of Service</a>
      <a href="/privacy">Privacy Policy</a>
      <a href="/pricing">Pricing</a>
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


def validate_address(address: str) -> dict:
    """
    Returns:
        {"valid": bool, "reason": str|None, "warnings": list[str], "normalized": str}
    """
    result = {"valid": False, "reason": None, "warnings": [], "normalized": ""}

    if not address or not address.strip():
        result["reason"] = "No address found in the message."
        return result

    cleaned = _re.sub(r"\s+", " ", address.strip())
    result["normalized"] = cleaned

    if not _STREET_NUMBER_RE.search(cleaned):
        result["reason"] = (
            f'"{cleaned}" doesn\'t start with a street number. '
            f"Include the full address, e.g. 1740 Grand Ave."
        )
        return result

    if not _STREET_SUFFIX_RE.search(cleaned):
        result["reason"] = (
            f'"{cleaned}" is missing a recognizable street type '
            f"(St, Ave, Rd, Blvd, Dr, Ln, etc). Double check the address."
        )
        return result

    if not _STATE_RE.search(cleaned) and not _TX_ZIP_RE.search(cleaned):
        result["warnings"].append("We couldn't verify that this property is in Texas. Please confirm before generating the final contract.")
    if len(cleaned.split()) < 3:
        result["warnings"].append("Address looks short. Please confirm city is included before signing.")

    result["valid"] = True
    return result



# --- stub MLS lookup ---------------------------------------------------
# Replace this with a real MLS API call (e.g. Bridge Interactive, Spark API)
# Real version should geocode address and query MLS for property data
def lookup_mls(address: str) -> dict:
    # Stub: derive varied placeholder data from address hash
    # Replace with real MLS API (Bridge Interactive, Spark, etc.) for production
    h = sum(ord(c) for c in address)
    return {
        "bed": (h % 4) + 2,
        "bath": (h % 3) + 1,
        "sqft": 1000 + (h % 20) * 150,
        "apn": "",
    }


def process_offer(incoming_msg: str, source_id: str):
    """Shared logic: parse -> validate address -> lookup MLS -> fill PDF.
    Returns (parsed, pdf_path_or_None, error_or_None, warnings)."""
    parsed = parse_offer_sms(incoming_msg)
    if "error" in parsed:
        return parsed, None, parsed["error"], []

    addr_check = validate_address(parsed.get("address", ""))
    if not addr_check["valid"]:
        return parsed, None, addr_check["reason"], []
    parsed["address"] = addr_check["normalized"]
    warnings = addr_check["warnings"]

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

    try:
        pdf_path = fill_offer_pdf(parsed, source_id)
    except Exception as e:
        return parsed, None, f"Parsed OK but couldn't generate the PDF yet: {e}", warnings

    return parsed, pdf_path, None, warnings


@app.route("/sms", methods=["GET", "POST"])
def sms_reply():
    if request.method == "GET":
        return redirect("/")

    incoming_msg = request.values.get("Body", "")
    agent_phone = request.values.get("From", "")

    # Log all incoming SMS for debugging
    print(f"[SMS] From: {agent_phone}, Body: {incoming_msg}")
    track_event("sms_received", agent_phone, {"body": incoming_msg})

    resp = MessagingResponse()

    # Handle keywords
    keyword = incoming_msg.strip().upper()

    if keyword in ("HELP", "MENU"):
        user = get_user(agent_phone)
        offer_count = user["offer_count"] if user else 0
        resp.message(
            "TxtAnOffer Commands:\n\n"
            "HELP - This menu\n"
            "DASHBOARD - Get link to your offer history\n"
            "STATUS - Check your plan & usage\n"
            "PROFILE - Get link to edit your agent info\n"
            "STOP - Unsubscribe from messages\n\n"
            "To generate an offer, text:\n"
            "price down% days address\n\n"
            "Example:\n"
            "725k 3% 21day 1740 Grand Ave, Austin TX 78701"
        )
        return Response(str(resp), mimetype="application/xml")

    if keyword == "DASHBOARD":
        dash_link = sign_dashboard_url(agent_phone, request.host_url.rstrip("/"))
        resp.message(f"Your dashboard (valid 7 days):\n{dash_link}")
        return Response(str(resp), mimetype="application/xml")

    if keyword == "STATUS":
        user = get_user(agent_phone)
        if not user:
            resp.message("No account found. Sign up at txtanoffer.com/signup")
        elif user["is_subscribed"]:
            resp.message(f"Plan: Unlimited\nOffers generated: {user['offer_count']}\n\nText HELP for commands.")
        else:
            remaining = max(0, FREE_OFFER_LIMIT - user["offer_count"])
            resp.message(f"Plan: Free trial\nOffers used: {user['offer_count']}/{FREE_OFFER_LIMIT}\nRemaining: {remaining}\n\nUpgrade: txtanoffer.com/pricing")
        return Response(str(resp), mimetype="application/xml")

    if keyword == "PROFILE":
        profile_link = request.host_url.rstrip("/") + f"/profile?phone={agent_phone}"
        resp.message(f"Edit your agent profile:\n{profile_link}\n\nYour name, license, brokerage, and defaults auto-fill into every contract.")
        return Response(str(resp), mimetype="application/xml")

    try:
        # Check subscription status
        can_generate, reason, user = can_generate_offer(agent_phone)
        print(f"[SMS] Subscription check: can_generate={can_generate}, reason={reason}")

        if not can_generate:
            # Track paywall hit
            track_event("limit_reached", agent_phone)

            # Send payment link
            payment_url = request.host_url.rstrip("/") + "/pricing"
            reply = (
                f"You've used your {FREE_OFFER_LIMIT} free offers! 🎉\n\n"
                f"Subscribe for unlimited offers:\n"
                f"{payment_url}\n\n"
                f"$49/mo • Cancel anytime\n"
                f"Saves 45min per offer"
            )
            resp.message(reply)
            return Response(str(resp), mimetype="application/xml")

        # Process offer
        parsed, pdf_path, error, warnings = process_offer(incoming_msg, agent_phone)

        if error:
            resp.message(error)
            return Response(str(resp), mimetype="application/xml")

        # Track offer generation
        track_event("offer_generated", agent_phone, {"price": parsed.get("price")})

        # Increment usage count
        new_count = increment_offer_count(agent_phone)

        # Check if trial just completed
        if new_count == FREE_OFFER_LIMIT and reason == "free_trial":
            track_event("trial_completed", agent_phone)

        filename = os.path.basename(pdf_path)
        pdf_url = sign_pdf_url(filename, request.host_url.rstrip("/"))

        record_offer(agent_phone, parsed, filename)

        # Fire webhook if configured
        fire_webhook(agent_phone, parsed, pdf_url)

        warning_line = f"\nNote: {' / '.join(warnings)}" if warnings else ""

        # Status line based on subscription
        if reason == "subscribed":
            status_line = ""
        else:
            remaining = FREE_OFFER_LIMIT - new_count
            if remaining > 0:
                status_line = f"\n✨ {remaining} free offers remaining"
            else:
                payment_url = request.host_url.rstrip("/") + "/pricing"
                status_line = f"\n🎉 Last free offer! Subscribe for unlimited:\n{payment_url}"

        reply = (
            f"Offer ready for {parsed['address']}\n\n"
            f"💰 Price: ${parsed['price']:,}\n"
            f"📅 Close: {parsed['close_days']} days\n"
            f"💵 Down: ${parsed['down_payment_amount']:,} ({parsed['down_payment_pct']*100:.0f}%)\n"
            f"🏦 Loan: ${parsed['loan_amount']:,}\n"
            f"✅ Earnest: ${parsed['earnest_money']:,}\n"
            f"🎯 Option: ${parsed['option_fee']}\n"
            f"\n"
            f"⚡️ Generated in <1s (vs 45min manual)\n"
            f"{warning_line}\n"
            f"Review: {pdf_url}\n"
            f"{status_line}\n\n"
            f"Share with your team:\n"
            f"txtanoffer.com/demo\n"
            f"(TREC 20-19 draft -- agent must review before signing)"
        )
        resp.message(reply)
        print(f"[SMS] Sending reply, length: {len(reply)} chars")
        return Response(str(resp), mimetype="application/xml")

    except Exception as e:
        print(f"[SMS] ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        resp.message("Error generating offer. Please try again or contact support.")
        return Response(str(resp), mimetype="application/xml")


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
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
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
  .nav-logo {{width:34px;height:34px;border-radius:50%;overflow:hidden;}}
  .nav-logo img {{width:100%;height:100%;object-fit:cover;}}
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
  .page {{max-width:580px;margin:0 auto;padding:4rem 1.5rem;}}
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
    padding:2rem;
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

  .pdf-preview {{margin-top:1.25rem;border:1px solid var(--border);border-radius:var(--radius-sm);overflow:hidden;}}
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
  .modal-close {{position:absolute;top:0.75rem;right:1rem;background:none;border:none;font-size:1.5rem;
    color:var(--text-dim);cursor:pointer;}}
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
    .page h1 {{font-size:1.75rem;}}
    .workflow {{flex-direction:column;gap:1rem;}}
    .wf-arrow {{transform:rotate(90deg);}}
    .nav-links {{display:none;}}
  }}
</style>
</head>
<body>
  <nav class="nav">
    <div class="nav-left">
      <div class="nav-logo"><img src="/static/logo.webp" alt="TxtAnOffer"></div>
      <span>TxtAnOffer</span>
    </div>
    <div class="nav-links">
      <a href="/">Home</a>
      <a href="/pricing">Pricing</a>
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
        <div class="hint">price &middot; down % &middot; closing days &middot; county (optional) &middot; address</div>
      </form>
      {result_html}
    </div>

    <div class="trust">
      <div class="trust-item"><div class="trust-val">&lt;10s</div><div class="trust-label">Generation</div></div>
      <div class="trust-item"><div class="trust-val">45 min</div><div class="trust-label">Saved per offer</div></div>
      <div class="trust-item"><div class="trust-val">TREC</div><div class="trust-label">20-19 Compliant</div></div>
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
        parsed, pdf_path, error, warnings = process_offer(offer_text, "demo-web")

        if error:
            result_html = f'<div class="error">{error}</div>'
        else:
            filename = os.path.basename(pdf_path)
            pdf_url = sign_pdf_url(filename)
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
            share_text = "Just generated a TREC 20-19 contract in 3 seconds by texting an address 🤯 TxtAnOffer turns '725k 3% 21day 1740 Grand Ave' into a filled PDF instantly."
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
              <div class="result-ready">Ready for review.</div>
              {warning_html}
              <div class="pdf-preview">
                <div class="pdf-preview-label">Contract preview</div>
                <iframe src="{pdf_url}#page=1&view=FitV" class="pdf-frame"></iframe>
                <div class="pdf-mobile"><a href="{pdf_url}" target="_blank">Tap to view your completed TREC 20-19 &rarr;</a></div>
              </div>
              <a href="{pdf_url}" target="_blank" class="download-btn">&darr; Download PDF</a>
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
                  body: JSON.stringify({{to_email: to, pdf_filename: filename, parsed: {parsed_json}}})
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

              <div class="disclaimer">Draft only -- agent must review before signing. TREC NO. 20-19.</div>

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


# --- Integration endpoints -------------------------------------------------

@app.route("/api/send-email", methods=["POST"])
def api_send_email():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "JSON body required"}), 400

    to_email = data.get("to_email", "")
    pdf_filename = data.get("pdf_filename", "")
    parsed = data.get("parsed", {})

    if not to_email or not pdf_filename:
        return jsonify({"success": False, "error": "to_email and pdf_filename required"}), 400

    pdf_path = os.path.join(OUTPUT_DIR, pdf_filename)
    if not os.path.exists(pdf_path):
        return jsonify({"success": False, "error": "PDF not found"}), 404

    result = send_offer_email(to_email, pdf_path, parsed)
    track_event("email_sent" if result["success"] else "email_failed", to_email, result)
    return jsonify(result), 200 if result["success"] else 500


@app.route("/api/webhook", methods=["GET", "POST", "DELETE"])
def api_webhook():
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
<meta name="description" content="TxtAnOffer pricing plans for Texas real estate agents. Generate TREC contracts instantly from $49/month.">
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
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
  .nav-logo {width:34px;height:34px;border-radius:50%;overflow:hidden;}
  .nav-logo img {width:100%;height:100%;object-fit:cover;}
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
    <div class="nav-logo"><img src="/static/logo.webp" alt="TxtAnOffer"></div>
    <span>TxtAnOffer</span>
  </a>
  <div class="nav-links">
    <a href="/">Home</a>
    <a href="/demo">Demo</a>
    <a href="/login">Log In</a>
  </div>
  <a href="/signup" class="nav-cta">Start Free Trial</a>
</nav>

<div class="page-header">
  <h1>Simple pricing.<br><span class="gradient">Massive time savings.</span></h1>
  <p>Stop spending 45 minutes per offer. Pick a plan and start generating contracts in seconds.</p>
</div>

<div class="pricing-grid">

  <div class="pricing-card">
    <h2 class="plan-name">Starter</h2>
    <p class="plan-desc">For individual agents getting started.</p>
    <div class="price-row">
      <span class="price-current">$49</span>
      <span class="price-period">/month</span>
    </div>
    <ul class="features">
      <li><span class="check">&#10003;</span> Unlimited offers</li>
      <li><span class="check">&#10003;</span> TREC 20-19 generation</li>
      <li><span class="check">&#10003;</span> SMS + Web access</li>
      <li><span class="check">&#10003;</span> Agent profile auto-fill</li>
      <li><span class="check">&#10003;</span> Email delivery</li>
    </ul>
    <form action="/create-checkout-session" method="POST">
      <input type="hidden" name="plan" value="starter">
      <button type="submit" class="cta-btn">Get Started</button>
    </form>
  </div>

  <div class="pricing-card featured">
    <span class="featured-badge">Most Popular</span>
    <h2 class="plan-name">Professional</h2>
    <p class="plan-desc">For active agents who close multiple deals monthly.</p>
    <div class="price-row">
      <span class="price-current">$99</span>
      <span class="price-period">/month</span>
    </div>
    <ul class="features">
      <li><span class="check">&#10003;</span> Everything in Starter</li>
      <li><span class="check">&#10003;</span> DocuSign integration</li>
      <li><span class="check">&#10003;</span> Webhook / CRM sync</li>
      <li><span class="check">&#10003;</span> Priority support</li>
      <li><span class="check">&#10003;</span> Custom cover page</li>
    </ul>
    <form action="/create-checkout-session" method="POST">
      <input type="hidden" name="plan" value="professional">
      <button type="submit" class="cta-btn">Get Professional</button>
    </form>
  </div>

  <div class="pricing-card">
    <h2 class="plan-name">Brokerage</h2>
    <p class="plan-desc">For teams and offices with multiple agents.</p>
    <div class="price-row">
      <span class="price-current">$299</span>
      <span class="price-period">/month</span>
    </div>
    <ul class="features">
      <li><span class="check">&#10003;</span> Everything in Professional</li>
      <li><span class="check">&#10003;</span> Up to 10 agent seats</li>
      <li><span class="check">&#10003;</span> Brokerage branding</li>
      <li><span class="check">&#10003;</span> Team analytics dashboard</li>
      <li><span class="check">&#10003;</span> Dedicated onboarding</li>
    </ul>
    <form action="/create-checkout-session" method="POST">
      <input type="hidden" name="plan" value="brokerage">
      <button type="submit" class="cta-btn">Get Brokerage</button>
    </form>
  </div>

  <div class="pricing-card">
    <h2 class="plan-name">Enterprise</h2>
    <p class="plan-desc">For large brokerages and franchises.</p>
    <div class="price-row">
      <span class="price-current">Custom</span>
    </div>
    <ul class="features">
      <li><span class="check">&#10003;</span> Everything in Brokerage</li>
      <li><span class="check">&#10003;</span> Unlimited seats</li>
      <li><span class="check">&#10003;</span> MLS integration</li>
      <li><span class="check">&#10003;</span> White-label option</li>
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
      <div class="value-title">Pays for Itself</div>
      <div class="value-text">Starter pays for itself with a single offer. Everything after is pure time savings.</div>
    </div>
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
    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Welcome to TxtAnOffer!</title>
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
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
  .logo img{{width:48px;height:48px;border-radius:50%;}}
</style>
</head>
<body>
  <div class="card">
    <div class="logo"><a href="/"><img src="/static/logo.webp" alt="TxtAnOffer"></a></div>
    <h1>Welcome aboard!</h1>
    <p class="sub">Your subscription is active. You're locked in at <strong>$49/month forever</strong>.</p>

    <div class="next-steps">
      <h3>Next Steps</h3>
      <ol>
        <li><strong>Set up your profile</strong> &mdash; your name, license, and brokerage auto-fill every offer</li>
        <li>Text your first offer to <strong>1-833-897-0333</strong></li>
        <li>Or use the web demo at <strong>txtanoffer.com/demo</strong></li>
      </ol>
    </div>

    <a href="/profile?phone={phone_from_checkout}" class="btn">Set Up Your Profile &rarr;</a>
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
        return jsonify(success=True)

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
    """Simple analytics dashboard (password protect in production!)"""
    metrics = get_conversion_metrics(days=30)
    revenue = get_revenue_metrics()
    recent_sms = get_recent_sms(limit=20)

    sms_rows = ""
    for sms in recent_sms:
        # Format timestamp
        from datetime import datetime
        dt = datetime.fromisoformat(sms['created_at'])
        time_str = dt.strftime("%m/%d %H:%M")
        sms_rows += f"<tr><td>{time_str}</td><td>{sms['phone']}</td><td>{sms['body'][:50]}</td></tr>"

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
<p style="color:#666;font-size:12px;margin-top:20px;">
  Check Twilio dashboard for full logs: <a href="https://console.twilio.com/us1/monitor/logs/sms" target="_blank">console.twilio.com/monitor/logs/sms</a>
</p>
</body></html>
"""


@app.route("/signup", methods=["GET", "POST"])
def signup():
    success_msg = ""
    if request.method == "POST":
        phone = request.form.get("phone", "")
        name = request.form.get("name", "")
        email = request.form.get("email", "")
        if phone:
            try:
                create_user(phone)
                track_event("signup", phone, {"name": name, "email": email})
                # Send welcome SMS
                from twilio.rest import Client
                twilio_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
                twilio_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
                twilio_from = os.environ.get("TWILIO_PHONE_NUMBER", "+18338970333")
                if twilio_sid and twilio_token:
                    client = Client(twilio_sid, twilio_token)
                    client.messages.create(
                        body=(
                            "Welcome to TxtAnOffer! "
                            "Text an offer like: 725k 3% 21day 123 Main St, Austin TX\n\n"
                            "Reply HELP for all commands. "
                            "Msg & data rates may apply. Reply STOP to opt out."
                        ),
                        from_=twilio_from,
                        to=phone,
                    )
            except Exception:
                pass
            success_msg = '<div class="success">You\'re signed up! Check your texts for a welcome message.</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sign Up — TxtAnOffer</title>
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
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
  .nav-back img{{width:28px;height:28px;border-radius:50%;}}
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
  .foot{{text-align:center;margin-top:1.5rem;font-size:0.8rem;color:var(--text-dim);}}
  .foot a{{color:var(--accent-light);text-decoration:none;}}
  .foot a:hover{{text-decoration:underline;}}
</style>
</head>
<body>
  <div class="wrap">
    <a href="/" class="nav-back"><img src="/static/logo.webp" alt=""><span>&larr; TxtAnOffer</span></a>
    <h1>Get started with TxtAnOffer</h1>
    <p class="sub">Enter your phone number to receive offer drafts via SMS at +1 (833) 897-0333.</p>
    <div class="card">
      <form method="POST" action="/signup" id="signup-form">
        <label class="field-label">Phone number</label>
        <input type="tel" name="phone" placeholder="+1 (555) 123-4567" required>
        <label class="field-label">Name</label>
        <input type="text" name="name" placeholder="Your name">
        <label class="field-label">Email</label>
        <input type="email" name="email" placeholder="you@brokerage.com">
        <div class="consent-row">
          <input type="checkbox" id="sms-consent" name="sms_consent" required>
          <label for="sms-consent">By checking this box, I agree to receive automated transactional SMS messages from TxtAnOffer at +1 (833) 897-0333 about my offer drafts. Message frequency varies based on usage. Reply STOP to opt out, HELP for help. Msg &amp; data rates may apply. Consent is not a condition of purchase. <a href="/privacy">Privacy Policy</a> &amp; <a href="/terms">Terms</a></label>
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
                from twilio.rest import Client
                twilio_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
                twilio_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
                twilio_from = os.environ.get("TWILIO_PHONE_NUMBER", "+18338970333")
                if twilio_sid and twilio_token:
                    client = Client(twilio_sid, twilio_token)
                    dash_link = sign_dashboard_url(phone_clean, request.host_url.rstrip("/"))
                    client.messages.create(
                        body=f"Your TxtAnOffer dashboard link (valid 7 days):\n{dash_link}",
                        from_=twilio_from,
                        to=phone_clean,
                    )
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
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
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
  .nav-back img{{width:28px;height:28px;border-radius:50%;}}
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
  <a href="/" class="nav-back"><img src="/static/logo.webp" alt=""><span>&larr; TxtAnOffer</span></a>
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
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Terms of Service — TxtAnOffer</title>
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=Inter:wght@400;500&display=swap" rel="stylesheet">
<style>
  *{box-sizing:border-box; margin:0; padding:0;}
  body{
    background:#fff;
    min-height:100vh; padding:48px 20px; font-family:'Inter',sans-serif;
    display:flex; justify-content:center;
  }
  .container{width:100%; max-width:680px;}
  .back-link{
    font-size:12px; color:#666; text-decoration:none;
    display:inline-block; margin-bottom:20px;
  }
  .back-link:hover{color:#000;}
  .page-title{
    font-family:'Source Serif 4',serif; font-weight:600; font-size:24px;
    color:#000; margin-bottom:4px;
  }
  .last-updated{
    font-size:12px; color:#999; margin-bottom:32px;
  }
  .card{
    padding:0;
  }
  .card h2{
    font-family:'Source Serif 4',serif; font-weight:600; font-size:15px;
    color:#000; margin:28px 0 10px; padding-bottom:6px;
    border-bottom:1px solid #e5e5e5;
  }
  .card h2:first-child{margin-top:0;}
  .card p, .card li{
    font-size:12px; line-height:1.7; color:#333; margin-bottom:8px;
  }
  .card ul{padding-left:18px; margin:6px 0 8px;}
  .card ul li{list-style:disc; margin-bottom:4px;}
  .card strong{color:#000; font-weight:500;}
  .card .emphasis{
    background:#f7f7f7; border-left:3px solid #000;
    padding:10px 14px; margin:12px 0; border-radius:0 2px 2px 0;
    font-size:12px; color:#000; line-height:1.6;
  }
  .section-num{
    color:#666; font-weight:500; margin-right:4px;
  }
  .foot{
    text-align:center; margin-top:24px; font-size:11px;
    color:#999; letter-spacing:0.02em;
  }
</style>
</head>
<body>
<div class="container">
  <div style="position:fixed;top:20px;left:24px;z-index:100;"><a href="/"><img src="/static/logo.webp" alt="TXTAnOffer" style="width:54px;height:54px;border-radius:50%;border:1.5px solid #C9A466;opacity:0.9;"></a></div>
  <a href="/" class="back-link">&larr; Back to TxtAnOffer</a>
  <h1 class="page-title">Terms of Service</h1>
  <p class="last-updated">Last Updated: July 12, 2026</p>
  <div class="card">
    <p>These Terms of Service ("Terms") govern your use of TxtAnOffer ("Service"), operated by Phanel ("we," "us," or "our"), a sole proprietorship based in Texas. By accessing or using the Service, you agree to be bound by these Terms. If you do not agree, do not use the Service.</p>

    <h2><span class="section-num">1.</span> Service Description</h2>
    <p>TxtAnOffer is a document drafting tool that converts shorthand offer text into pre-filled TREC One to Four Family Residential Contract (Resale) forms (TREC No. 20-19). The Service accepts offer parameters via SMS (Twilio) or a web interface and generates a partially completed PDF contract for review by a licensed Texas real estate agent.</p>
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
    <p>We use publicly available TREC promulgated forms as templates. If TREC revises or replaces a form, there may be a delay before we update the Service. You are responsible for confirming that the form version used is current and appropriate for your transaction.</p>

    <h2><span class="section-num">6.</span> Subscription, Payment, and Cancellation</h2>
    <p><strong>Pricing:</strong> The Service costs $49.00 per month, billed monthly via Stripe.</p>
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
    <p><strong>Third-party services:</strong> The Service uses Twilio (SMS delivery), Stripe (payment processing), and cloud hosting providers. These services have their own privacy policies and may process your data in accordance with their terms.</p>
    <p><strong>Data retention:</strong> Generated PDFs are stored temporarily and may be deleted after a reasonable period. We retain account and billing records as required by law.</p>
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
  <p class="foot">TxtAnOffer is not affiliated with the Texas Real Estate Commission (TREC).</p>
</div>
</body>
</html>"""


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
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=Inter:wght@400;500&display=swap" rel="stylesheet">
<style>
  *{box-sizing:border-box; margin:0; padding:0;}
  body{
    background:#fff;
    min-height:100vh; padding:48px 20px; font-family:'Inter',sans-serif;
    display:flex; justify-content:center;
  }
  .container{width:100%; max-width:680px;}
  .back-link{
    font-size:12px; color:#666; text-decoration:none;
    display:inline-block; margin-bottom:20px;
  }
  .back-link:hover{color:#000;}
  .page-title{
    font-family:'Source Serif 4',serif; font-weight:600; font-size:24px;
    color:#000; margin-bottom:4px;
  }
  .last-updated{
    font-size:12px; color:#999; margin-bottom:32px;
  }
  .card h2{
    font-family:'Source Serif 4',serif; font-weight:600; font-size:15px;
    color:#000; margin:28px 0 10px; padding-bottom:6px;
    border-bottom:1px solid #e5e5e5;
  }
  .card h2:first-child{margin-top:0;}
  .card p, .card li{
    font-size:12px; line-height:1.7; color:#333; margin-bottom:8px;
  }
  .card ul{padding-left:18px; margin:6px 0 8px;}
  .card ul li{list-style:disc; margin-bottom:4px;}
  .card strong{color:#000; font-weight:500;}
  .foot{
    text-align:center; margin-top:24px; font-size:11px;
    color:#999; letter-spacing:0.02em;
  }
</style>
</head>
<body>
<div class="container">
  <div style="position:fixed;top:20px;left:24px;z-index:100;"><a href="/"><img src="/static/logo.webp" alt="TXTAnOffer" style="width:54px;height:54px;border-radius:50%;border:1.5px solid #C9A466;opacity:0.9;"></a></div>
  <a href="/" class="back-link">&larr; Back to TxtAnOffer</a>
  <h1 class="page-title">Privacy Policy</h1>
  <p class="last-updated">Last Updated: July 14, 2026</p>
  <div class="card">
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
    <p><strong>Toll-Free Number:</strong> +1 (833) 897-0333</p>
    <p><strong>Opt-in Method:</strong> Users opt in by (1) entering their phone number and checking an unchecked checkbox on www.txtanoffer.com/signup that says "By checking this box, I agree to receive automated transactional SMS messages from TxtAnOffer at +1 (833) 897-0333 about my offer drafts. Message frequency varies based on usage. Reply STOP to opt out, HELP for help. Msg &amp; data rates may apply. Consent is not a condition of purchase." OR (2) by texting offer details directly to +1 (833) 897-0333 after seeing opt-in disclosure on our website.</p>
    <p><strong>Consent:</strong> By texting our service number +1 (833) 897-0333 or submitting your phone number via our website, you consent to receive SMS messages from TxtAnOffer related to your offer requests and account.</p>
    <p><strong>Message frequency:</strong> Message frequency varies based on your usage. You will receive one response per offer submitted, plus occasional account notifications (typically 1-5 messages per month).</p>
    <p><strong>Opt-out:</strong> Reply STOP to any message to unsubscribe from SMS. Reply START to re-subscribe. You can continue using the web interface after opting out of SMS.</p>
    <p><strong>Help:</strong> Reply HELP for support information, or contact support@txtanoffer.com or +1 (833) 897-0333.</p>
    <p><strong>Rates:</strong> Message and data rates may apply depending on your carrier plan.</p>
    <p><strong>Carriers:</strong> Compatible with all major US carriers. Carriers are not liable for delayed or undelivered messages.</p>
    <p>This is a transactional, user-initiated service only. We do not send marketing or promotional messages.</p>

    <h2>4. Data Sharing</h2>
    <p>We do not sell, rent, or trade your personal information. We share data only with:</p>
    <ul>
      <li><strong>Twilio</strong> — SMS delivery (phone number, message content)</li>
      <li><strong>Stripe</strong> — Payment processing (billing details)</li>
      <li><strong>Cloud hosting provider</strong> — Infrastructure (all data in transit and at rest)</li>
    </ul>
    <p>We may disclose information if required by law, legal process, or to protect the rights and safety of our users or the public.</p>

    <h2>5. Data Retention</h2>
    <ul>
      <li>Generated PDFs: stored temporarily for download, deleted after 30 days</li>
      <li>Account data: retained while your account is active and for 90 days after cancellation</li>
      <li>Billing records: retained as required by applicable tax and accounting laws</li>
      <li>SMS logs: retained for 90 days for support and debugging purposes</li>
    </ul>

    <h2>6. Data Security</h2>
    <p>We implement reasonable technical and organizational measures to protect your data, including encryption in transit (TLS), secure hosting, and access controls. However, no method of transmission over the internet is 100% secure, and we cannot guarantee absolute security.</p>

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
  <p class="foot">TxtAnOffer is not affiliated with the Texas Real Estate Commission (TREC).</p>
</div>
</body>
</html>"""


@app.route("/profile", methods=["GET", "POST"])
def profile():
    phone = request.args.get("phone", "").strip()
    saved = False
    error = ""

    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        if not phone:
            error = "Phone number is required."
        else:
            save_agent_profile(phone, {
                "name": request.form.get("name", "").strip(),
                "license": request.form.get("license", "").strip(),
                "phone": phone,
                "email": request.form.get("email", "").strip(),
                "brokerage": request.form.get("brokerage", "").strip(),
                "title_company": request.form.get("title_company", "").strip(),
                "default_earnest_pct": float(request.form.get("earnest_pct", "1")) / 100,
                "default_option_fee": int(request.form.get("option_fee", "250")),
            })
            saved = True

    existing = get_agent_profile(phone) if phone else {}

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Agent Profile - TxtAnOffer</title>
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=Inter:wght@400;500&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root{{--ink:#171B24;--ink-soft:#242938;--paper:#F3EEDF;--paper-line:#DCD3B8;
    --brass:#A9772F;--green:#3A5744;--text-on-paper:#211E17;--text-muted:#847C68;}}
  *{{box-sizing:border-box;}}
  body{{background:var(--ink);min-height:100vh;margin:0;display:flex;align-items:center;
    justify-content:center;padding:48px 20px;font-family:'Inter',sans-serif;}}
  .card{{background:var(--paper);border-radius:4px;padding:40px 36px;max-width:460px;width:100%;
    box-shadow:0 24px 60px -20px rgba(0,0,0,0.5);border-top:3px solid var(--brass);}}
  h1{{font-family:'Source Serif 4',serif;font-size:24px;color:var(--text-on-paper);margin:0 0 6px;}}
  .sub{{color:var(--text-muted);font-size:13px;margin:0 0 28px;line-height:1.5;}}
  label{{display:block;font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:0.08em;
    text-transform:uppercase;color:var(--text-muted);margin-bottom:6px;margin-top:16px;}}
  input{{width:100%;font-family:'Inter',sans-serif;font-size:14px;padding:11px 14px;
    border:1px solid var(--paper-line);background:#FFFDF7;color:var(--text-on-paper);
    border-radius:2px;outline:none;}}
  input:focus{{border-color:var(--brass);}}
  .row{{display:flex;gap:12px;}}
  .row > div{{flex:1;}}
  button{{width:100%;margin-top:24px;background:var(--ink);color:#E7E4D8;border:none;
    padding:14px;font-family:'Inter',sans-serif;font-size:14px;font-weight:500;
    border-radius:2px;cursor:pointer;}}
  button:hover{{background:var(--ink-soft);}}
  .success{{margin-top:16px;padding:12px;background:rgba(58,87,68,0.1);border:1px solid rgba(58,87,68,0.3);
    border-radius:2px;font-size:13px;color:var(--green);text-align:center;}}
  .error{{margin-top:16px;padding:12px;background:rgba(139,58,44,0.08);border:1px solid rgba(139,58,44,0.3);
    border-radius:2px;font-size:13px;color:#7A3527;text-align:center;}}
  .foot{{text-align:center;margin-top:20px;font-size:12px;}}
  .foot a{{color:var(--brass);text-decoration:none;}}
</style>
</head>
<body>
  <div class="card">
    <h1>Agent Profile</h1>
    <p class="sub">Your info auto-fills the cover page on every offer you generate.</p>
    <form method="POST" action="/profile">
      <label>Phone number (used for SMS offers)</label>
      <input type="text" name="phone" placeholder="+15125551234" value="{phone or existing.get('phone', '')}" required>

      <label>Full name</label>
      <input type="text" name="name" placeholder="Jane Smith" value="{existing.get('name', '')}">

      <label>TREC license number</label>
      <input type="text" name="license" placeholder="0123456" value="{existing.get('license', '')}">

      <label>Email</label>
      <input type="email" name="email" placeholder="jane@realty.com" value="{existing.get('email', '')}">

      <label>Brokerage</label>
      <input type="text" name="brokerage" placeholder="Keller Williams" value="{existing.get('brokerage', '')}">

      <label>Title company</label>
      <input type="text" name="title_company" placeholder="Texas Title Co." value="{existing.get('title_company', '')}">

      <div class="row">
        <div>
          <label>Default earnest %</label>
          <input type="number" name="earnest_pct" step="0.1" min="0.1" max="10" value="{existing.get('default_earnest_pct', 0.01) * 100:.1f}">
        </div>
        <div>
          <label>Default option fee $</label>
          <input type="number" name="option_fee" min="0" max="5000" value="{existing.get('default_option_fee', 250)}">
        </div>
      </div>

      <button type="submit">Save Profile</button>
    </form>
    {'<div class="success">Profile saved! Your info will appear on all future offers.</div>' if saved else ''}
    {'<div class="error">' + error + '</div>' if error else ''}
    <div class="foot"><a href="/demo">&larr; Back to demo</a></div>
  </div>
</body>
</html>
"""


@app.route("/offers/<path:filename>")
def serve_offer(filename):
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
    return f"{base_url}/dashboard?phone={phone}&expires={expires}&sig={sig}"


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
<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Dashboard - TxtAnOffer</title>
<style>body{font-family:'Inter',sans-serif;background:#171B24;color:#E7E4D8;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px;}
.box{background:#242938;border-radius:8px;padding:40px;max-width:400px;text-align:center;
border:1px solid rgba(169,119,47,0.15);}
h2{margin:0 0 12px;font-size:22px;}p{color:#8B8A82;font-size:14px;line-height:1.6;}
a{color:#C9A466;}</style></head><body><div class="box">
<h2>Access Expired</h2>
<p>Your dashboard link has expired or is invalid.<br>
Text <strong>DASHBOARD</strong> to (833) 897-0333 to get a fresh link.</p>
<p><a href="/">Back to home</a></p></div></body></html>""", 403

    user = get_user(phone)
    if not user:
        return redirect("/signup")

    from agent_profiles import get_agent_profile
    agent = get_agent_profile(phone)
    offers = get_offers_for_phone(phone)
    from datetime import timedelta

    # Build offer rows
    offer_rows = ""
    for o in offers:
        pdf_link = sign_pdf_url(o["filename"], request.host_url.rstrip("/"))
        created = o["created_at"][:10]
        offer_rows += f"""
        <tr>
          <td>{o['address']}</td>
          <td>${o['price']:,}</td>
          <td>{o['down_pct']*100:.0f}%</td>
          <td>{o['close_days']}d</td>
          <td>{created}</td>
          <td><a href="{pdf_link}" target="_blank">PDF</a></td>
        </tr>"""

    if not offer_rows:
        offer_rows = '<tr><td colspan="6" style="text-align:center;color:#8B8A82;padding:24px;">No offers yet. Text your first offer to get started.</td></tr>'

    sub_status = "Active" if user["is_subscribed"] else f"Free ({user['offer_count']}/{FREE_OFFER_LIMIT} used)"
    sub_badge_color = "#3A5744" if user["is_subscribed"] else "#A9772F"

    # Reusable dashboard link (same params, still valid)
    dash_url = f"/dashboard?phone={phone}&expires={expires}&sig={sig}"
    profile_url = f"/profile?phone={phone}"

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard - TxtAnOffer</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=Inter:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root{{--ink:#171B24;--ink-soft:#242938;--paper:#F3EEDF;--brass:#A9772F;--brass-soft:#C9A466;
    --green:#3A5744;--text-on-ink:#E7E4D8;--text-on-ink-muted:#8B8A82;}}
  *{{box-sizing:border-box;}}
  body{{background:var(--ink);font-family:'Inter',sans-serif;color:var(--text-on-ink);margin:0;padding:0;min-height:100vh;}}
  .dash-nav{{display:flex;align-items:center;justify-content:space-between;padding:20px 32px;max-width:1100px;margin:0 auto;}}
  .dash-nav a{{color:var(--text-on-ink-muted);text-decoration:none;font-size:14px;}}
  .dash-nav a:hover{{color:var(--text-on-ink);}}
  .dash-nav .logo{{font-family:'Source Serif 4',serif;font-weight:600;font-size:20px;color:var(--text-on-ink);text-decoration:none;}}
  .container{{max-width:1000px;margin:0 auto;padding:0 24px 60px;}}
  .greeting{{font-family:'Source Serif 4',serif;font-size:28px;font-weight:600;margin:32px 0 8px;}}
  .sub-badge{{display:inline-block;background:{sub_badge_color};color:#fff;padding:4px 12px;border-radius:12px;font-size:12px;font-weight:600;}}
  .stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:16px;margin:32px 0;}}
  .stat{{background:var(--ink-soft);border-radius:8px;padding:20px;border:1px solid rgba(169,119,47,0.1);}}
  .stat-val{{font-family:'Source Serif 4',serif;font-size:28px;font-weight:600;color:var(--brass);}}
  .stat-label{{font-size:12px;color:var(--text-on-ink-muted);margin-top:4px;}}
  h2{{font-family:'Source Serif 4',serif;font-size:22px;margin:40px 0 16px;}}
  .table-wrap{{overflow-x:auto;}}
  table{{width:100%;border-collapse:collapse;font-size:14px;}}
  th{{text-align:left;padding:10px 12px;border-bottom:1px solid rgba(255,255,255,0.1);
    color:var(--text-on-ink-muted);font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:0.05em;}}
  td{{padding:12px;border-bottom:1px solid rgba(255,255,255,0.05);}}
  td a{{color:var(--brass-soft);text-decoration:none;}}
  td a:hover{{text-decoration:underline;}}
  .actions{{display:flex;gap:12px;margin-top:24px;flex-wrap:wrap;}}
  .actions a{{background:var(--ink-soft);color:var(--text-on-ink);padding:10px 20px;border-radius:4px;
    text-decoration:none;font-size:14px;font-weight:500;border:1px solid rgba(169,119,47,0.15);transition:border-color 0.2s;}}
  .actions a:hover{{border-color:var(--brass);}}
  @media(max-width:600px){{.stats{{grid-template-columns:1fr 1fr;}}.greeting{{font-size:22px;}}}}
</style>
</head>
<body>
<nav class="dash-nav">
  <a href="/" class="logo">TxtAnOffer</a>
  <div>
    <a href="{profile_url}">Edit Profile</a>
  </div>
</nav>
<div class="container">
  <div class="greeting">Welcome back{', ' + agent.get('name').split()[0] if agent.get('name') else ''}</div>
  <span class="sub-badge">{sub_status}</span>

  <div class="stats">
    <div class="stat"><div class="stat-val">{user['offer_count']}</div><div class="stat-label">Total offers</div></div>
    <div class="stat"><div class="stat-val">{len(offers)}</div><div class="stat-label">In history</div></div>
    <div class="stat"><div class="stat-val">{user['offer_count'] * 45}m</div><div class="stat-label">Time saved</div></div>
  </div>

  <h2>Offer History</h2>
  <div class="table-wrap">
  <table>
    <tr><th>Address</th><th>Price</th><th>Down</th><th>Close</th><th>Date</th><th>PDF</th></tr>
    {offer_rows}
  </table>
  </div>

  <div class="actions">
    <a href="{profile_url}">Agent Profile</a>
    <a href="/pricing">{'Manage Subscription' if user['is_subscribed'] else 'Upgrade Plan'}</a>
  </div>
</div>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
