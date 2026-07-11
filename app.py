"""
app.py — Twilio SMS webhook for TextAnOffer, plus a /demo web form that
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
from agent_profiles import get_agent_profile
from subscriptions import can_generate_offer, increment_offer_count, activate_subscription, deactivate_subscription, FREE_OFFER_LIMIT
from analytics import track_event, get_conversion_metrics, get_revenue_metrics, get_recent_sms

app = Flask(__name__)

# Stripe configuration
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")  # Your $49/mo price ID from Stripe dashboard


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
        result["warnings"].append("No TX or Texas ZIP code detected -- confirm this is the right state.")
    if len(cleaned.split()) < 3:
        result["warnings"].append("Address looks short -- confirm city is included.")

    result["valid"] = True
    return result



# --- stub MLS lookup ---------------------------------------------------
# Replace this with a real MLS API call (e.g. Bridge Interactive, Spark API)
# Real version should geocode address and query MLS for property data
def lookup_mls(address: str) -> dict:
    # Stub: Default to Austin, Travis County for demo
    # Real implementation should use geocoding API or MLS data
    return {
        "bed": 3,
        "bath": 2,
        "sqft": 1450,
        "apn": "714-123-45",
        "city": "Austin",
        "county": "Travis",
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

    # Use agent-specified county/city if provided, otherwise use MLS lookup or defaults
    if "county" not in parsed:
        parsed["county"] = mls_data.get("county", "Travis")
    if "city" not in parsed:
        parsed["city"] = mls_data.get("city", "Austin")

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
            f"🏠 Property: {parsed['bed']}bed/{parsed['bath']}bath/{parsed['sqft']:,}sf\n\n"
            f"⚡️ Generated in <1s (vs 45min manual)\n"
            f"{warning_line}\n"
            f"Review: {pdf_url}\n"
            f"{status_line}\n\n"
            f"Share with your team:\n"
            f"textanoffer-production.up.railway.app/demo\n"
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
<title>TextAnOffer</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=Inter:wght@400;500&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root{{
    --ink:#1a1a1a; --ink-soft:#2a2a2a; --paper:#FAF8F5; --paper-line:#E8E2D9;
    --brass:#8C6D3F; --brass-soft:#A8874F; --green:#3A5744;
    --text-on-paper:#1a1a1a; --text-muted:#7A7468;
    --text-on-ink:#F5F3F0; --text-on-ink-muted:#9E9A94;
    --marble-1:#f5f0eb; --marble-2:#ebe5de; --marble-3:#d9d0c5;
  }}
  *{{box-sizing:border-box;}}
  body{{
    background:var(--marble-1);
    background-image:
      linear-gradient(135deg, rgba(200,190,175,0.15) 0%, transparent 50%),
      linear-gradient(225deg, rgba(180,170,155,0.1) 0%, transparent 40%),
      radial-gradient(ellipse at 20% 50%, rgba(200,185,165,0.2), transparent 50%),
      radial-gradient(ellipse at 80% 20%, rgba(190,180,165,0.15), transparent 45%),
      radial-gradient(ellipse at 60% 80%, rgba(210,195,175,0.12), transparent 40%);
    min-height:100vh; margin:0; display:flex; align-items:center; justify-content:center;
    padding:48px 20px; font-family:'Inter',sans-serif;
  }}
  .stage{{width:100%;max-width:440px;text-align:center;}}
  .corner-mark{{display:flex;justify-content:space-between;font-family:'IBM Plex Mono',monospace;
    font-size:10px;letter-spacing:0.08em;color:var(--text-muted);margin-bottom:14px;padding:0 4px;}}
  .corner-mark span.brass{{color:var(--brass);}}
  h1{{font-family:'Source Serif 4',serif;font-weight:600;font-size:28px;color:var(--text-on-paper);
    margin:0 0 6px;letter-spacing:-0.02em;min-height:70px;text-transform:uppercase;}}
  .sub{{color:var(--text-muted);font-size:13px;line-height:1.6;margin:0 auto 32px;max-width:360px;}}
  .card{{background:var(--paper);border-radius:2px;padding:36px 32px 32px;
    box-shadow:0 8px 40px -12px rgba(0,0,0,0.12);border-top:none;
    border:1px solid var(--paper-line);}}
  .divider{{width:60px;height:1px;background:var(--paper-line);margin:18px auto 22px;}}
  .field-label{{font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:0.1em;
    text-transform:uppercase;color:var(--text-muted);margin-bottom:10px;display:block;text-align:left;}}
  input[type=text]{{width:100%;font-family:'IBM Plex Mono',monospace;font-size:13px;padding:14px 16px;
    border:1px solid var(--paper-line);background:#FFFFFF;color:var(--text-on-paper);
    border-radius:2px;outline:none;text-align:center;}}
  input[type=text]:focus{{border-color:var(--brass);}}
  button{{width:100%;margin-top:16px;background:var(--ink);color:var(--text-on-ink);border:none;
    padding:14px;font-family:'Inter',sans-serif;font-size:13px;font-weight:500;border-radius:2px;
    cursor:pointer;letter-spacing:0.04em;text-transform:uppercase;}}
  button:hover{{background:var(--ink-soft);}}
  .hint{{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--text-muted);margin-top:12px;
    letter-spacing:0.02em;}}
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
  .warning-note{{margin:2px 0 10px;padding:8px 12px;background:rgba(169,119,47,0.1);
    border:1px solid rgba(169,119,47,0.3);border-radius:2px;font-size:12px;color:#8A6423;}}
  .foot{{text-align:center;margin-top:24px;font-family:'IBM Plex Mono',monospace;font-size:10px;
    color:var(--text-muted);letter-spacing:0.04em;}}
  .foot a{{color:var(--brass);text-decoration:none;}}
  .foot a:hover{{text-decoration:underline;}}
</style>
</head>
<body>
  <div class="stage">
    <div class="card">
      <h1 id="headline"></h1>
      <p style="font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:0.12em;text-transform:uppercase;color:var(--text-muted);margin:0 0 4px;">REAL ESTATE OFFERS</p>
      <div class="divider"></div>
      <p class="sub" style="margin:0 auto 24px;text-align:center;">Type an offer the way you'd text it.<br>Get a real TREC 20-19 contract back.</p>
      <script>
        const lines = ['TextAnOffer'];
        const headline = document.getElementById('headline');
        let lineIdx = 0, charIdx = 0, currentText = '';

        function type() {{{{
          if (lineIdx >= lines.length) return;
          const line = lines[lineIdx];
          if (charIdx < line.length) {{{{
            currentText += line[charIdx];
            headline.textContent = currentText;
            charIdx++;
            setTimeout(type, 90);
          }}}} else {{{{
            if (lineIdx < lines.length - 1) {{{{
              setTimeout(() => {{{{ lineIdx++; charIdx = 0; type(); }}}}, 400);
            }}}}
          }}}}
        }}}}
        type();
      </script>
      <form method="POST" action="/demo">
        <input type="text" name="offer_text" placeholder="725k 3% 21day 1740 Grand Ave" value="{prefill}">
        <button type="submit">Generate Offer</button>
        <div class="hint">price &middot; down % &middot; close days &middot; address</div>
      </form>
      {result_html}
    </div>
    <div class="foot">
      <a href="/pricing">View Pricing</a>
    </div>
  </div>
</body>
</html>
"""


@app.route("/demo", methods=["GET", "POST"])
def demo():
    result_html = ""
    prefill = ""

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
                warning_html = f'<div class="warning-note">{" / ".join(warnings)}</div>'
            # Social share URLs
            share_text = "Just generated a TREC 20-19 contract in 3 seconds by texting an address 🤯 TextAnOffer turns '725k 3% 21day 1740 Grand Ave' into a filled PDF instantly."
            share_url = "https://textanoffer-production.up.railway.app/demo"
            twitter_share = f"https://twitter.com/intent/tweet?text={share_text.replace(' ', '%20')}&url={share_url}"
            linkedin_share = f"https://www.linkedin.com/sharing/share-offsite/?url={share_url}"

            result_html = f"""
            <div class="result">
              <div class="result-stamp">Ready to sign</div>
              <div class="result-addr">{parsed['address']}</div>
              <div class="result-row"><span class="k">Sales price</span><span class="v">${parsed['price']:,}</span></div>
              <div class="result-row"><span class="k">Down payment</span><span class="v">${parsed['down_payment_amount']:,} ({parsed['down_payment_pct']*100:.0f}%)</span></div>
              <div class="result-row"><span class="k">Loan amount</span><span class="v">${parsed['loan_amount']:,}</span></div>
              <div class="result-row"><span class="k">Earnest money</span><span class="v">${parsed['earnest_money']:,}</span></div>
              <div class="result-row"><span class="k">Option fee</span><span class="v">${parsed['option_fee']}</span></div>
              <div class="result-row"><span class="k">Closing date</span><span class="v">{close_date_str}</span></div>
              <div class="result-row"><span class="k">Property</span><span class="v">{parsed['bed']}bed / {parsed['bath']}bath / {parsed['sqft']:,}sqft</span></div>
              {warning_html}
              <a href="{pdf_url}" target="_blank" class="download-btn">Download filled TREC 20-19 &rarr;</a>
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

    return DEMO_FORM.format(prefill=prefill, result_html=result_html)


@app.route("/pricing")
def pricing():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pricing - TextAnOffer</title>
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
  .container{max-width:900px;margin:0 auto;}
  .header{text-align:center;margin-bottom:48px;}
  .logo{font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:0.08em;
    color:var(--brass-soft);margin-bottom:16px;}
  h1{font-family:'Source Serif 4',serif;font-weight:600;font-size:42px;color:var(--text-on-ink);
    margin:0 0 12px;letter-spacing:-0.01em;}
  .tagline{color:var(--text-on-ink-muted);font-size:18px;line-height:1.6;max-width:600px;margin:0 auto;}

  .pricing-card{background:var(--paper);border-radius:4px;padding:40px;margin-bottom:24px;
    border-top:3px solid var(--brass);box-shadow:0 24px 60px -20px rgba(0,0,0,0.5);}
  .plan-badge{display:inline-block;font-family:'IBM Plex Mono',monospace;font-size:10px;
    letter-spacing:0.08em;text-transform:uppercase;color:var(--brass);
    background:rgba(169,119,47,0.15);border:1px solid rgba(169,119,47,0.4);
    padding:4px 10px;border-radius:20px;margin-bottom:16px;}
  .plan-name{font-family:'Source Serif 4',serif;font-size:28px;font-weight:600;
    color:var(--text-on-paper);margin:0 0 8px;}
  .price-row{display:flex;align-items:baseline;gap:8px;margin-bottom:16px;}
  .price-original{font-size:24px;color:var(--text-muted);text-decoration:line-through;}
  .price-current{font-size:48px;font-weight:600;color:var(--text-on-paper);}
  .price-period{font-size:18px;color:var(--text-muted);}
  .price-note{font-size:13px;color:var(--brass);margin-bottom:24px;font-weight:500;}

  .features{list-style:none;padding:0;margin:0 0 32px;}
  .features li{padding:12px 0;border-bottom:1px solid var(--paper-line);font-size:15px;
    color:var(--text-on-paper);display:flex;align-items:start;gap:12px;}
  .features li:last-child{border:none;}
  .check{color:var(--green);font-weight:600;}

  .cta-btn{display:block;width:100%;padding:16px;background:var(--ink);color:var(--text-on-ink);
    border:none;font-family:'Inter',sans-serif;font-size:16px;font-weight:600;
    border-radius:4px;cursor:pointer;text-decoration:none;text-align:center;}
  .cta-btn:hover{background:var(--ink-soft);}

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
</style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div class="logo">TEXTANOFFER</div>
      <h1>Simple pricing.<br>Massive time savings.</h1>
      <p class="tagline">Join Texas agents saving 45 minutes per offer with instant TREC 20-19 generation.</p>
    </div>

    <div class="pricing-card">
      <span class="plan-badge">🚀 Early Adopter Pricing</span>
      <h2 class="plan-name">Professional Plan</h2>
      <div class="price-row">
        <span class="price-original">$79</span>
        <span class="price-current">$49</span>
        <span class="price-period">/month</span>
      </div>
      <p class="price-note">Lock in this rate forever • Normally $79/mo</p>

      <ul class="features">
        <li><span class="check">✓</span> <strong>Unlimited offers</strong> — Generate as many as you need</li>
        <li><span class="check">✓</span> <strong>Instant calculations</strong> — Down payment, loan amount, earnest money auto-filled</li>
        <li><span class="check">✓</span> <strong>Complete TREC 20-19</strong> — All 281 fields, professional output</li>
        <li><span class="check">✓</span> <strong>SMS + Web access</strong> — Text or use demo page</li>
        <li><span class="check">✓</span> <strong>Agent profile</strong> — Your info auto-fills every time</li>
        <li><span class="check">✓</span> <strong>No contracts</strong> — Cancel anytime</li>
      </ul>

      <form action="/create-checkout-session" method="POST">
        <button type="submit" class="cta-btn">Get Early Access →</button>
      </form>
    </div>

    <div class="value-props">
      <div class="value-card">
        <div class="value-title">Time ROI</div>
        <div class="value-text">Save 45 minutes per offer. At 5 offers/month, you save 3.75 hours — worth $187-$562 of your time.</div>
      </div>
      <div class="value-card">
        <div class="value-title">Zero Errors</div>
        <div class="value-text">Math calculated automatically. No more "$21,750 or 3%?" double-checking. Every field consistent.</div>
      </div>
      <div class="value-card">
        <div class="value-title">Pays for Itself</div>
        <div class="value-text">Break even with just 2 offers per month. Everything after that is pure time savings.</div>
      </div>
    </div>

    <div class="back-link">
      <a href="/demo">← Back to demo</a>
    </div>
  </div>
</body>
</html>
"""


@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    """Create Stripe checkout session for subscription"""
    if not stripe.api_key or not STRIPE_PRICE_ID:
        # Fallback if Stripe not configured: email signup
        return redirect("mailto:hello@textanoffer.com?subject=Early%20Adopter%20Signup")

    try:
        checkout_session = stripe.checkout.Session.create(
            line_items=[{
                'price': STRIPE_PRICE_ID,
                'quantity': 1,
            }],
            mode='subscription',
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
    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Welcome to TextAnOffer!</title>
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
    <h1>🎉 Welcome aboard!</h1>
    <p>Your subscription is active. You're locked in at <strong>$49/month forever</strong>.</p>

    <div class="next-steps">
      <h3>Next Steps:</h3>
      <ol>
        <li>Check your email for your receipt and account details</li>
        <li>Text your first offer to <strong>1-833-897-0333</strong></li>
        <li>Or use the web demo at <strong>textanoffer.com/demo</strong></li>
        <li>Your agent profile will be set up within 24 hours</li>
      </ol>
    </div>

    <a href="/demo" class="btn">Generate Your First Offer →</a>
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
        customer_id = session['customer']
        subscription_id = session['subscription']

        # Track conversion
        track_event("subscription_created", metadata={
            "customer_id": customer_id,
            "email": customer_email
        })

        # NOTE: Phone number linking happens manually for now
        # In production: add phone field to checkout or link via email

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
<html><head><title>TextAnOffer Analytics</title>
<style>
body{{font-family:system-ui;max-width:800px;margin:40px auto;padding:20px;}}
.metric{{background:#f5f5f5;padding:20px;margin:10px 0;border-radius:8px;}}
.metric h3{{margin:0 0 10px;color:#333;}}
.metric .value{{font-size:32px;font-weight:bold;color:#A9772F;}}
.metric .label{{color:#666;font-size:14px;}}
</style></head><body>
<h1>TextAnOffer Analytics</h1>
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


@app.route("/offers/<path:filename>")
def serve_offer(filename):
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=False)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
