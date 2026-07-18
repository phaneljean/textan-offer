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

from flask import Flask, request, send_from_directory, Response, redirect, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from datetime import datetime
import os
import stripe

from parser import parse_offer_sms
from pdf_filler import fill_offer_pdf, OUTPUT_DIR
from agent_profiles import get_agent_profile, save_agent_profile
from subscriptions import can_generate_offer, increment_offer_count, activate_subscription, deactivate_subscription, get_user, create_user, FREE_OFFER_LIMIT
from analytics import track_event, get_conversion_metrics, get_revenue_metrics, get_recent_sms
from integrations import send_offer_email, fire_webhook, save_webhook, get_webhook, delete_webhook, send_to_docusign

app = Flask(__name__)

# Stripe configuration
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")
STRIPE_PRICE_ID_PRO = os.environ.get("STRIPE_PRICE_ID_PRO", "")
STRIPE_PRICE_ID_BROKERAGE = os.environ.get("STRIPE_PRICE_ID_BROKERAGE", "")

 @app.route("/")
  def index():
      return redirect("/signup")


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
    # Stub: returns placeholder property data only
    # Real implementation should geocode the address and query MLS
    return {
        "bed": 3,
        "bath": 2,
        "sqft": 1450,
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


@app.route("/sms", methods=["POST"])
def sms_reply():
    incoming_msg = request.values.get("Body", "")
    agent_phone = request.values.get("From", "")

    # Log all incoming SMS for debugging
    print(f"[SMS] From: {agent_phone}, Body: {incoming_msg}")
    track_event("sms_received", agent_phone, {"body": incoming_msg})

    resp = MessagingResponse()

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
        pdf_url = request.host_url.rstrip("/") + f"/offers/{filename}"

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
<title>TxtAnOffer</title>
<meta name="description" content="Generate TREC purchase offers in 10 seconds via text or web. Texas real estate agents save 45 minutes per offer.">
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=Inter:wght@400;500&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root{{
    --ink:#171B24; --ink-soft:#242938; --paper:#F3EEDF; --paper-line:#DCD3B8;
    --brass:#A9772F; --brass-soft:#C9A466; --green:#3A5744;
    --text-on-paper:#211E17; --text-muted:#847C68;
    --text-on-ink:#E7E4D8; --text-on-ink-muted:#8B8A82;
  }}
  *{{box-sizing:border-box;}}
  body{{
    background:var(--ink);
    background-image:radial-gradient(circle at 15% 10%, rgba(169,119,47,0.06), transparent 45%),
                      radial-gradient(circle at 85% 90%, rgba(169,119,47,0.04), transparent 40%);
    min-height:100vh; margin:0; display:flex; align-items:center; justify-content:center;
    padding:48px 20px; font-family:'Inter',sans-serif;
  }}
  .stage{{width:100%;max-width:460px;}}
  .site-logo{{position:fixed;top:20px;left:24px;z-index:100;}}
  .site-logo img{{width:54px;height:54px;border-radius:50%;border:1.5px solid var(--brass-soft);
    opacity:0.9;transition:opacity 0.2s;}}
  .site-logo img:hover{{opacity:1;}}
  .corner-mark{{display:flex;justify-content:space-between;font-family:'IBM Plex Mono',monospace;
    font-size:10.5px;letter-spacing:0.06em;color:var(--text-on-ink-muted);margin-bottom:14px;padding:0 4px;}}
  .corner-mark span.brass{{color:var(--brass-soft);}}
  h1{{font-family:'Source Serif 4',serif;font-weight:600;font-size:32px;color:var(--text-on-ink);
    margin:0 0 6px;letter-spacing:-0.01em;min-height:80px;}}
  .sub{{color:var(--text-on-ink-muted);font-size:14px;line-height:1.55;margin:0 0 32px;max-width:380px;}}
  .card{{background:var(--paper);border-radius:2px;padding:28px 26px 26px;
    box-shadow:0 24px 60px -20px rgba(0,0,0,0.5);border-top:2px solid var(--brass);}}
  .field-label{{font-family:'IBM Plex Mono',monospace;font-size:10.5px;letter-spacing:0.08em;
    text-transform:uppercase;color:var(--text-muted);margin-bottom:8px;display:block;}}
  input[type=text]{{width:100%;font-family:'IBM Plex Mono',monospace;font-size:14px;padding:13px 14px;
    border:1px solid var(--paper-line);background:#FFFDF7;color:var(--text-on-paper);
    border-radius:2px;outline:none;}}
  input[type=text]:focus{{border-color:var(--brass);}}
  button{{width:100%;margin-top:14px;background:var(--ink);color:var(--text-on-ink);border:none;
    padding:14px;font-family:'Inter',sans-serif;font-size:14px;font-weight:500;border-radius:2px;
    cursor:pointer;letter-spacing:0.01em;}}
  button:hover{{background:var(--ink-soft);}}
  .hint{{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--text-muted);margin-top:10px;}}
  .result{{margin-top:22px;padding-top:20px;border-top:1px dashed var(--paper-line);}}
  .result-stamp{{display:inline-flex;align-items:center;gap:6px;font-family:'IBM Plex Mono',monospace;
    font-size:10px;letter-spacing:0.08em;text-transform:uppercase;color:var(--green);
    background:rgba(58,87,68,0.1);border:1px solid rgba(58,87,68,0.35);padding:4px 10px;
    border-radius:20px;margin-bottom:14px;}}
  .result-addr{{font-family:'Source Serif 4',serif;font-size:19px;color:var(--text-on-paper);margin:0 0 12px;}}
  .result-row{{display:flex;justify-content:space-between;font-size:13.5px;padding:7px 0;
    border-bottom:1px solid rgba(220,211,184,0.6);}}
  .result-row .k{{color:var(--text-muted);font-family:'IBM Plex Mono',monospace;font-size:11px;
    text-transform:uppercase;letter-spacing:0.04em;}}
  .result-row .v{{color:var(--text-on-paper);font-weight:500;}}
  .result-ready{{font-size:13px;color:var(--green);font-style:italic;margin-top:14px;padding-top:12px;
    border-top:1px dashed var(--paper-line);}}
  .pdf-preview{{margin-top:18px;border:1px solid var(--paper-line);border-radius:2px;overflow:hidden;}}
  .pdf-preview-label{{font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:0.06em;
    text-transform:uppercase;color:var(--text-muted);padding:8px 12px;background:rgba(220,211,184,0.3);
    border-bottom:1px solid var(--paper-line);}}
  .pdf-frame{{width:100%;height:560px;border:none;background:#fff;}}
  .pdf-mobile{{display:none;padding:20px;text-align:center;background:#FFFDF7;}}
  .pdf-mobile a{{color:var(--brass);font-weight:500;font-size:14px;text-decoration:none;}}
  .pdf-mobile a:hover{{text-decoration:underline;}}
  @media(max-width:768px){{
    .pdf-frame{{display:none;}}
    .pdf-mobile{{display:block;}}
    .site-logo{{position:static;margin-bottom:16px;text-align:center;}}
    .site-logo img{{width:48px;height:48px;}}
  }}
  .download-btn{{margin-top:18px;display:block;text-align:center;background:var(--brass);color:#2A1D08;
    text-decoration:none;font-weight:500;font-size:14px;padding:13px;border-radius:2px;}}
  .download-btn:hover{{background:var(--brass-soft);}}
  .disclaimer{{margin-top:14px;font-size:11.5px;color:var(--text-muted);line-height:1.5;font-style:italic;}}
  .share-section{{margin-top:20px;padding-top:18px;border-top:1px dashed var(--paper-line);}}
  .share-label{{font-family:'IBM Plex Mono',monospace;font-size:10.5px;letter-spacing:0.08em;
    text-transform:uppercase;color:var(--text-muted);margin-bottom:10px;display:block;text-align:center;}}
  .share-buttons{{display:flex;gap:8px;justify-content:center;}}
  .share-btn{{flex:1;max-width:140px;padding:10px 14px;text-align:center;text-decoration:none;
    border-radius:2px;font-size:13px;font-weight:500;transition:opacity 0.2s;display:flex;
    align-items:center;justify-content:center;gap:6px;}}
  .share-btn:hover{{opacity:0.85;}}
  .share-twitter{{background:#1DA1F2;color:white;}}
  .share-linkedin{{background:#0A66C2;color:white;}}
  .share-copy{{background:var(--ink-soft);color:var(--text-on-ink);cursor:pointer;border:1px solid rgba(255,255,255,0.1);}}
  .share-copy.copied{{background:var(--green);border-color:var(--green);}}
  .error{{margin-top:22px;padding:14px 16px;background:rgba(139,58,44,0.08);
    border:1px solid rgba(139,58,44,0.3);border-radius:2px;font-size:13px;color:#7A3527;}}
  .warning-note{{margin:14px 0 10px;padding:12px 14px;background:rgba(169,119,47,0.08);
    border:1px solid rgba(169,119,47,0.25);border-radius:4px;font-size:12.5px;color:#6B5220;line-height:1.5;}}
  .warning-note .wn-title{{font-family:'IBM Plex Mono',monospace;font-size:10.5px;font-weight:500;
    letter-spacing:0.04em;text-transform:uppercase;margin-bottom:4px;color:var(--brass);}}
  .workflow{{display:flex;align-items:center;justify-content:center;gap:0;margin:0 0 30px;padding:0 4px;}}
  .wf-step{{text-align:center;flex:1;}}
  .wf-icon{{font-size:22px;margin-bottom:6px;}}
  .wf-title{{font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:0.06em;
    text-transform:uppercase;color:var(--text-on-ink);font-weight:500;}}
  .wf-desc{{font-size:11px;color:var(--text-on-ink-muted);margin-top:3px;line-height:1.4;}}
  .wf-arrow{{color:var(--brass);font-size:16px;margin:0 2px;flex-shrink:0;opacity:0.7;}}
  .integration-actions{{display:flex;gap:8px;margin:18px 0 0;flex-wrap:wrap;}}
  .int-btn{{flex:1;min-width:120px;padding:10px 12px;font-size:12px;font-weight:500;border:1px solid var(--paper-line);
    background:#FFFDF7;color:var(--text-on-paper);border-radius:2px;cursor:pointer;
    font-family:'IBM Plex Mono',monospace;letter-spacing:0.02em;transition:border-color 0.2s;}}
  .int-btn:hover{{border-color:var(--brass);}}
  .modal{{position:fixed;inset:0;background:rgba(23,27,36,0.85);display:flex;align-items:center;
    justify-content:center;z-index:1000;padding:20px;}}
  .modal-box{{background:var(--paper);padding:28px 24px;border-radius:4px;width:100%;max-width:360px;
    position:relative;border-top:2px solid var(--brass);}}
  .modal-title{{font-family:'Source Serif 4',serif;font-size:18px;font-weight:600;color:var(--text-on-paper);margin:0 0 16px;}}
  .modal-desc{{font-size:13px;color:var(--text-muted);margin:0 0 12px;line-height:1.5;}}
  .modal-input{{width:100%;font-family:'IBM Plex Mono',monospace;font-size:13px;padding:11px 12px;
    border:1px solid var(--paper-line);background:#FFFDF7;color:var(--text-on-paper);
    border-radius:2px;outline:none;margin-bottom:10px;}}
  .modal-input:focus{{border-color:var(--brass);}}
  .modal-submit{{width:100%;padding:12px;background:var(--ink);color:var(--text-on-ink);border:none;
    font-family:'Inter',sans-serif;font-size:13px;font-weight:500;border-radius:2px;cursor:pointer;}}
  .modal-submit:hover{{background:var(--ink-soft);}}
  .modal-close{{position:absolute;top:12px;right:14px;background:none;border:none;font-size:20px;
    color:var(--text-muted);cursor:pointer;}}
  .modal-status{{margin-top:10px;font-size:12px;color:var(--text-muted);font-family:'IBM Plex Mono',monospace;}}
  .modal-status.success{{color:var(--green);}}
  .modal-status.fail{{color:#7A3527;}}
  .trust-checks{{display:flex;flex-wrap:wrap;gap:8px 16px;margin-top:24px;padding:0 4px;}}
  .trust-check{{font-family:'IBM Plex Mono',monospace;font-size:11.5px;color:var(--text-on-ink);
    letter-spacing:0.02em;}}
  .trust-tagline{{font-family:'Source Serif 4',serif;font-size:13px;font-style:italic;
    color:var(--brass-soft);margin-top:14px;padding:0 4px;}}
  .trust{{display:flex;gap:16px;margin-top:28px;padding:0 4px;}}
  .trust-item{{flex:1;text-align:center;}}
  .trust-val{{font-family:'Source Serif 4',serif;font-size:20px;font-weight:600;color:var(--brass);}}
  .trust-label{{font-family:'IBM Plex Mono',monospace;font-size:9.5px;letter-spacing:0.06em;
    text-transform:uppercase;color:var(--text-on-ink-muted);margin-top:4px;}}
  .foot{{text-align:center;margin-top:24px;font-family:'IBM Plex Mono',monospace;font-size:10.5px;
    color:var(--text-on-ink-muted);letter-spacing:0.03em;}}
  .foot a{{color:var(--brass-soft);text-decoration:none;}}
  .foot a:hover{{text-decoration:underline;}}
</style>
</head>
<body>
  <div class="stage">
    <div class="site-logo"><a href="/"><img src="/static/logo.webp" alt="TXTAnOffer"></a></div>
    <h1 id="headline"></h1>
    <p class="sub">Agents spend up to 45 minutes preparing purchase offers. TxtAnOffer reduces that to under 10 seconds.</p>
    <div class="workflow">
      <div class="wf-step"><div class="wf-icon">&#9993;</div><div class="wf-title">You type</div><div class="wf-desc">725k 3% 21day<br>1234 Main St</div></div>
      <div class="wf-arrow">&rarr;</div>
      <div class="wf-step"><div class="wf-icon">&#9881;</div><div class="wf-title">We parse</div><div class="wf-desc">Price, terms &amp;<br>address extracted</div></div>
      <div class="wf-arrow">&rarr;</div>
      <div class="wf-step"><div class="wf-icon">&#9998;</div><div class="wf-title">Contract ready</div><div class="wf-desc">TREC 20-19 PDF<br>filled &amp; downloadable</div></div>
    </div>
    <script>
      const lines = ['Get a purchase offer', 'in 10 seconds.'];
      const headline = document.getElementById('headline');
      let lineIdx = 0, charIdx = 0, currentText = '';

      function type() {{{{
        if (lineIdx >= lines.length) return;

        const line = lines[lineIdx];
        if (charIdx < line.length) {{{{
          currentText += line[charIdx];
          headline.innerHTML = currentText + (lineIdx === 0 ? '' : '');
          charIdx++;
          setTimeout(type, 80);
        }}}} else {{{{
          if (lineIdx < lines.length - 1) {{{{
            setTimeout(() => {{{{
              currentText += '<br>';
              lineIdx++;
              charIdx = 0;
              type();
            }}}}, 400);
          }}}}
        }}}}
      }}}}
      type();
    </script>
    <div class="card">
      <form method="POST" action="/demo">
        <label class="field-label">Offer details</label>
        <input type="text" name="offer_text" placeholder="725k 3% 21day Harris 1234 Westheimer Rd" value="{prefill}">
        <button type="submit">Generate My Contract</button>
        <div class="hint">price &middot; down % &middot; closing days &middot; county (optional) &middot; address</div>
      </form>
      {result_html}
    </div>
    <div class="trust-checks">
      <div class="trust-check">&check; Official TREC 20-19</div>
      <div class="trust-check">&check; Agent reviews before signing</div>
      <div class="trust-check">&check; Texas compliant</div>
    </div>
    <div class="trust-tagline">Built specifically for Texas REALTORS&reg;</div>
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
            pdf_url = f"/offers/{filename}"
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
              function sendEmail(filename) {{{{
                const to = document.getElementById('email-to').value;
                const status = document.getElementById('email-status');
                if (!to) {{{{ status.textContent = 'Enter an email address'; return; }}}}
                status.textContent = 'Sending...';
                fetch('/api/send-email', {{{{
                  method: 'POST',
                  headers: {{{{'Content-Type': 'application/json'}}}},
                  body: JSON.stringify({{{{to_email: to, pdf_filename: filename, parsed: {parsed_json}}}}})
                }}}}).then(r => r.json()).then(d => {{{{
                  status.textContent = d.success ? 'Sent!' : ('Error: ' + d.error);
                  status.className = 'modal-status ' + (d.success ? 'success' : 'fail');
                }}}}).catch(e => {{{{ status.textContent = 'Network error'; }}}});
              }}}}

              function sendDocuSign(filename) {{{{
                const name = document.getElementById('ds-name').value;
                const email = document.getElementById('ds-email').value;
                const status = document.getElementById('ds-status');
                if (!name || !email) {{{{ status.textContent = 'Name and email required'; return; }}}}
                status.textContent = 'Sending to DocuSign...';
                fetch('/api/docusign', {{{{
                  method: 'POST',
                  headers: {{{{'Content-Type': 'application/json'}}}},
                  body: JSON.stringify({{{{pdf_filename: filename, signer_email: email, signer_name: name, parsed: {parsed_json}}}}})
                }}}}).then(r => r.json()).then(d => {{{{
                  status.textContent = d.success ? 'Sent! Envelope: ' + d.envelope_id : ('Error: ' + d.error);
                  status.className = 'modal-status ' + (d.success ? 'success' : 'fail');
                }}}}).catch(e => {{{{ status.textContent = 'Network error'; }}}});
              }}}}

              function configWebhook() {{{{
                const url = document.getElementById('wh-url').value;
                const status = document.getElementById('wh-status');
                if (!url) {{{{ status.textContent = 'Enter a webhook URL'; return; }}}}
                status.textContent = 'Saving...';
                fetch('/api/webhook', {{{{
                  method: 'POST',
                  headers: {{{{'Content-Type': 'application/json'}}}},
                  body: JSON.stringify({{{{source_id: 'demo-web', url: url}}}})
                }}}}).then(r => r.json()).then(d => {{{{
                  status.textContent = d.success ? 'Webhook saved! Future offers will POST here.' : ('Error: ' + (d.error || ''));
                  status.className = 'modal-status ' + (d.success ? 'success' : 'fail');
                }}}}).catch(e => {{{{ status.textContent = 'Network error'; }}}});
              }}}}
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
                    setTimeout(()=>{{{{this.textContent='🔗 Copy link';this.classList.remove('copied');}}}},2000)
                  ">🔗 Copy link</button>
                </div>
              </div>
            </div>
            """

    return DEMO_FORM.format(prefill=prefill, result_html=result_html, date_stamp=date_stamp)


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
<title>Pricing - TxtAnOffer</title>
<meta name="description" content="TxtAnOffer pricing plans for Texas real estate agents. Generate TREC contracts instantly from $49/month.">
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=Inter:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root{
    --ink:#171B24; --ink-soft:#242938; --paper:#F3EEDF; --paper-line:#DCD3B8;
    --brass:#A9772F; --brass-soft:#C9A466; --green:#3A5744;
    --text-on-paper:#211E17; --text-muted:#847C68;
    --text-on-ink:#E7E4D8; --text-on-ink-muted:#8B8A82;
  }
  *{box-sizing:border-box;}
  body{
    background:var(--ink);
    background-image:radial-gradient(circle at 15% 10%, rgba(169,119,47,0.06), transparent 45%),
                      radial-gradient(circle at 85% 90%, rgba(169,119,47,0.04), transparent 40%);
    min-height:100vh; margin:0; padding:48px 20px; font-family:'Inter',sans-serif;
  }
  .container{max-width:1000px;margin:0 auto;}
  .header{text-align:center;margin-bottom:48px;}
  .logo{position:fixed;top:20px;left:24px;z-index:100;}
  .logo img{width:54px;height:54px;border-radius:50%;border:1.5px solid var(--brass-soft);
    opacity:0.9;transition:opacity 0.2s;}
  .logo img:hover{opacity:1;}
  h1{font-family:'Source Serif 4',serif;font-weight:600;font-size:42px;color:var(--text-on-ink);
    margin:0 0 12px;letter-spacing:-0.01em;}
  .tagline{color:var(--text-on-ink-muted);font-size:18px;line-height:1.6;max-width:600px;margin:0 auto;}

  .pricing-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:20px;margin-bottom:48px;}
  .pricing-card{background:var(--paper);border-radius:4px;padding:32px 28px;
    border-top:3px solid var(--paper-line);box-shadow:0 24px 60px -20px rgba(0,0,0,0.5);
    display:flex;flex-direction:column;}
  .pricing-card.featured{border-top-color:var(--brass);position:relative;}
  .featured-badge{position:absolute;top:-12px;left:50%;transform:translateX(-50%);
    font-family:'IBM Plex Mono',monospace;font-size:9.5px;letter-spacing:0.08em;
    text-transform:uppercase;color:var(--brass);background:var(--paper);
    border:1px solid rgba(169,119,47,0.4);padding:3px 10px;border-radius:20px;white-space:nowrap;}

  .plan-name{font-family:'Source Serif 4',serif;font-size:22px;font-weight:600;
    color:var(--text-on-paper);margin:0 0 6px;}
  .plan-desc{font-size:13px;color:var(--text-muted);margin:0 0 20px;line-height:1.4;}
  .price-row{display:flex;align-items:baseline;gap:4px;margin-bottom:20px;}
  .price-current{font-size:40px;font-weight:600;color:var(--text-on-paper);}
  .price-period{font-size:14px;color:var(--text-muted);}

  .features{list-style:none;padding:0;margin:0 0 24px;flex:1;}
  .features li{padding:8px 0;font-size:13.5px;color:var(--text-on-paper);
    display:flex;align-items:start;gap:8px;}
  .check{color:var(--green);font-weight:600;font-size:14px;}

  .cta-btn{display:block;width:100%;padding:14px;background:var(--ink);color:var(--text-on-ink);
    border:none;font-family:'Inter',sans-serif;font-size:14px;font-weight:600;
    border-radius:4px;cursor:pointer;text-decoration:none;text-align:center;}
  .cta-btn:hover{background:var(--ink-soft);}
  .cta-btn.brass{background:var(--brass);color:#2A1D08;}
  .cta-btn.brass:hover{background:var(--brass-soft);}
  .cta-btn.outline{background:transparent;border:1px solid var(--paper-line);color:var(--text-on-paper);}
  .cta-btn.outline:hover{border-color:var(--brass);}

  .value-props{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:24px;
    margin-top:48px;}
  .value-card{background:rgba(243,238,223,0.08);padding:24px;border-radius:4px;
    border:1px solid rgba(243,238,223,0.12);}
  .value-title{font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:0.08em;
    text-transform:uppercase;color:var(--brass-soft);margin-bottom:8px;}
  .value-text{color:var(--text-on-ink-muted);font-size:14px;line-height:1.6;}

  .back-link{text-align:center;margin-top:32px;}
  .back-link a{color:var(--brass-soft);text-decoration:none;font-size:14px;}
  .back-link a:hover{text-decoration:underline;}
  .terms-note{text-align:center;font-size:12px;color:var(--text-on-ink-muted);margin-top:20px;}
  .terms-note a{color:var(--brass-soft);text-decoration:underline;}
</style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div class="logo"><a href="/"><img src="/static/logo.webp" alt="TXTAnOffer"></a></div>
      <h1>Simple pricing.<br>Massive time savings.</h1>
      <p class="tagline">Stop spending 45 minutes per offer. Pick a plan and start generating contracts in seconds.</p>
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
          <button type="submit" class="cta-btn brass">Get Professional</button>
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

    <p class="terms-note">All plans cancel anytime. No contracts. By subscribing you agree to our <a href="/terms">Terms of Service</a>.</p>

    <div class="value-props">
      <div class="value-card">
        <div class="value-title">Time ROI</div>
        <div class="value-text">Save 45 minutes per offer. At 5 offers/month, that's 3.75 hours back — worth $187-$562 of your time.</div>
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

    <div class="back-link">
      <a href="/demo">&larr; Back to demo</a>
    </div>
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
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=Inter:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root{{--ink:#171B24;--paper:#F3EEDF;--brass:#A9772F;--green:#3A5744;}}
  body{{background:var(--ink);min-height:100vh;margin:0;display:flex;align-items:center;
    justify-content:center;padding:20px;font-family:'Inter',sans-serif;}}
  .card{{background:var(--paper);padding:48px;border-radius:4px;max-width:500px;text-align:center;
    border-top:3px solid var(--green);}}
  h1{{font-family:'Source Serif 4',serif;font-size:32px;margin:0 0 16px;color:#211E17;}}
  p{{color:#847C68;font-size:16px;line-height:1.6;margin-bottom:24px;}}
  .next-steps{{text-align:left;background:#FFFDF7;padding:20px;border-radius:4px;margin-bottom:24px;}}
  .next-steps h3{{font-size:14px;text-transform:uppercase;letter-spacing:0.05em;margin:0 0 12px;}}
  .next-steps ol{{margin:0;padding-left:20px;}}
  .next-steps li{{margin:8px 0;font-size:14px;}}
  .btn{{display:inline-block;padding:14px 32px;background:var(--ink);color:#E7E4D8;
    text-decoration:none;border-radius:4px;font-weight:500;}}
  .btn:hover{{background:#242938;}}
</style>
</head>
<body>
  <div class="card">
    <div style="position:fixed;top:20px;left:24px;z-index:100;"><a href="/"><img src="/static/logo.webp" alt="TXTAnOffer" style="width:54px;height:54px;border-radius:50%;border:1.5px solid #C9A466;opacity:0.9;"></a></div>
    <h1>🎉 Welcome aboard!</h1>
    <p>Your subscription is active. You're locked in at <strong>$49/month forever</strong>.</p>

    <div class="next-steps">
      <h3>Next Steps:</h3>
      <ol>
        <li><strong>Set up your profile</strong> — your name, license, and brokerage auto-fill every offer</li>
        <li>Text your first offer to <strong>1-833-897-0333</strong></li>
        <li>Or use the web demo at <strong>txtanoffer.com/demo</strong></li>
      </ol>
    </div>

    <a href="/profile?phone={phone_from_checkout}" class="btn">Set Up Your Profile →</a>
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
@app.route("/signup", methods=["GET", "POST"])
def signup():
      success_msg = ""if request.method == "POST"
          phone = request.form.get("phone", "")
          name = request.form.get("name", "")
          email = request.form.get("email", "")if phone:try:
                  create_user(phone)
                  track_event("signup", phone, {"name": name, "email": email})
              except Exception:
                  pass
              success_msg = '<div class="success">You\'re signed up! Text your offer details to <strong>+1 (833) 897-0333</strong> to get started.</div>'

      return f"""<!DOCTYPE html>
  <html lang="en">
  <head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Sign Up — TxtAnOffer</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=Inter:wght@400;500&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root{{
      --ink:#171B24; --ink-soft:#242938; --paper:#F3EEDF; --paper-line:#DCD3B8;
      --brass:#A9772F; --brass-soft:#C9A466; --green:#3A5744;
      --text-on-paper:#211E17; --text-muted:#847C68;
      --text-on-ink:#E7E4D8; --text-on-ink-muted:#8B8A82;
    }}
    *{{box-sizing:border-box;}}
    body{{
      background:var(--ink);
      background-image:radial-gradient(circle at 15% 10%, rgba(169,119,47,0.06), transparent 45%),
                        radial-gradient(circle at 85% 90%, rgba(169,119,47,0.04), transparent 40%);
      min-height:100vh; margin:0; display:flex; align-items:center; justify-content:center;
      padding:48px 20px; font-family:'Inter',sans-serif;
    }}
    .stage{{width:100%;max-width:460px;}}
    .corner-mark{{display:flex;justify-content:space-between;font-family:'IBM Plex Mono',monospace;
      font-size:10.5px;letter-spacing:0.06em;color:var(--text-on-ink-muted);margin-bottom:14px;padding:0 4px;}}
    .corner-mark span.brass{{color:var(--brass-soft);}}
    .corner-mark a{{color:var(--text-on-ink-muted);text-decoration:none;}}
    .corner-mark a:hover{{color:var(--brass-soft);}}
    h1{{font-family:'Source Serif 4',serif;font-weight:600;font-size:28px;color:var(--text-on-ink);
      margin:0 0 6px;letter-spacing:-0.01em;}}
    .sub{{color:var(--text-on-ink-muted);font-size:14px;line-height:1.55;margin:0 0 32px;max-width:380px;}}
    .card{{background:var(--paper);border-radius:2px;padding:28px 26px 26px;
      box-shadow:0 24px 60px -20px rgba(0,0,0,0.5);border-top:2px solid var(--brass);}}
    .field-label{{font-family:'IBM Plex Mono',monospace;font-size:10.5px;letter-spacing:0.08em;
      text-transform:uppercase;color:var(--text-muted);margin-bottom:8px;display:block;}}
    input[type=text],input[type=tel],input[type=email]{{width:100%;font-family:'IBM Plex Mono',monospace;
      font-size:14px;padding:13px 14px;border:1px solid var(--paper-line);background:#FFFDF7;
      color:var(--text-on-paper);border-radius:2px;outline:none;margin-bottom:16px;}}
    input:focus{{border-color:var(--brass);}}
    .consent-row{{display:flex;align-items:flex-start;gap:10px;margin:18px 0;padding:14px;
      background:rgba(169,119,47,0.06);border:1px solid rgba(169,119,47,0.15);border-radius:2px;}}
    .consent-row input[type=checkbox]{{margin-top:3px;width:18px;height:18px;flex-shrink:0;accent-color:var(--brass);}}
    .consent-row label{{font-size:13px;line-height:1.6;color:var(--text-on-paper);}}
    .consent-row a{{color:var(--brass);text-decoration:underline;}}
    button{{width:100%;margin-top:14px;background:var(--ink);color:var(--text-on-ink);border:none;
      padding:14px;font-family:'Inter',sans-serif;font-size:14px;font-weight:500;border-radius:2px;
      cursor:pointer;letter-spacing:0.01em;}}
    button:hover{{background:var(--ink-soft);}}
    button:disabled{{opacity:0.4;cursor:not-allowed;}}
    .success{{margin-top:20px;padding:16px;background:rgba(58,87,68,0.1);border:1px solid rgba(58,87,68,0.3);
      border-radius:2px;font-size:14px;color:var(--green);text-align:center;}}
    .foot{{text-align:center;margin-top:24px;font-family:'IBM Plex Mono',monospace;font-size:10.5px;
      color:var(--text-on-ink-muted);letter-spacing:0.03em;}}
    .foot a{{color:var(--text-on-ink-muted);text-decoration:underline;}}
    .foot a:hover{{color:var(--brass-soft);}}
  </style>
  </head>
  <body>
    <div class="stage">
      <div class="corner-mark"><a href="/">TEXTANOFFER</a><span class="brass">SIGN UP</span></div>
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
            <label for="sms-consent">I agree to receive transactional SMS messages from TxtAnOffer at (833) 897-0333 for offer drafts. Message frequency varies. Reply STOP to opt-out, HELP for help.
  Message &amp; data rates may apply. Consent is not a condition of purchase. <a href="/privacy">Privacy Policy</a></label>
          </div>
          <button type="submit">Sign up for SMS</button>
        </form>
        {success_msg}
      </div>
      <div class="foot"><a href="/privacy">Privacy Policy</a> &middot; <a href="/terms">Terms</a> &middot; <a href="/demo">Try the demo</a></div>
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
    <p><strong>Opt-in Method:</strong> Users opt in by (1) entering their phone number and checking an unchecked checkbox on www.txtanoffer.com that says "By checking this box and texting +1 (833) 897-0333, I agree to receive automated transactional messages from TxtAnOffer about my offer drafts. Msg &amp; data rates may apply. Reply STOP to opt out." OR (2) by texting offer details directly to +1 (833) 897-0333 after seeing opt-in disclosure on our website.</p>
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
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=False)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
