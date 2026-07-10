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

from flask import Flask, request, send_from_directory, Response
from twilio.twiml.messaging_response import MessagingResponse
from datetime import datetime
import os

from parser import parse_offer_sms
from pdf_filler import fill_offer_pdf, OUTPUT_DIR

app = Flask(__name__)


# --- stub MLS lookup ---------------------------------------------------
# Replace this with a real MLS API call (e.g. Bridge Interactive, Spark API)
def lookup_mls(address: str) -> dict:
    return {
        "bed": 3,
        "bath": 2,
        "sqft": 1450,
        "apn": "714-123-45",
    }


def process_offer(incoming_msg: str, source_id: str):
    """Shared logic: parse -> lookup MLS -> fill PDF. Returns (parsed, pdf_path_or_None, error_or_None)."""
    parsed = parse_offer_sms(incoming_msg)
    if "error" in parsed:
        return parsed, None, parsed["error"]

    mls_data = lookup_mls(parsed["address"])
    parsed.update(mls_data)

    try:
        pdf_path = fill_offer_pdf(parsed, source_id)
    except Exception as e:
        return parsed, None, f"Parsed OK but couldn't generate the PDF yet: {e}"

    return parsed, pdf_path, None


@app.route("/sms", methods=["POST"])
def sms_reply():
    incoming_msg = request.values.get("Body", "")
    agent_phone = request.values.get("From", "")

    resp = MessagingResponse()
    parsed, pdf_path, error = process_offer(incoming_msg, agent_phone)

    if error:
        resp.message(error)
        return Response(str(resp), mimetype="application/xml")

    filename = os.path.basename(pdf_path)
    pdf_url = request.host_url.rstrip("/") + f"/offers/{filename}"

    reply = (
        f"Offer ready for {parsed['address']}\n"
        f"Price: ${parsed['price']:,}\n"
        f"Close: {parsed['close_days']} days\n"
        f"Generated in <1s\n\n"
        f"Review: {pdf_url}\n"
        f"(TREC 20-19 draft -- agent must review before signing)"
    )
    resp.message(reply)
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
  h1{{font-family:'Source Serif 4',serif;font-weight:600;font-size:32px;color:var(--text-on-ink);
    margin:0 0 6px;letter-spacing:-0.01em;}}
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
  .download-btn{{margin-top:18px;display:block;text-align:center;background:var(--brass);color:#2A1D08;
    text-decoration:none;font-weight:500;font-size:14px;padding:13px;border-radius:2px;}}
  .download-btn:hover{{background:var(--brass-soft);}}
  .disclaimer{{margin-top:14px;font-size:11.5px;color:var(--text-muted);line-height:1.5;font-style:italic;}}
  .error{{margin-top:22px;padding:14px 16px;background:rgba(139,58,44,0.08);
    border:1px solid rgba(139,58,44,0.3);border-radius:2px;font-size:13px;color:#7A3527;}}
  .foot{{text-align:center;margin-top:24px;font-family:'IBM Plex Mono',monospace;font-size:10.5px;
    color:var(--text-on-ink-muted);letter-spacing:0.03em;}}
</style>
</head>
<body>
  <div class="stage">
    <div class="corner-mark"><span>TEXTANOFFER</span><span class="brass">{date_stamp}</span></div>
    <h1>Text a price.<br>Get a real offer.</h1>
    <p class="sub">Type an offer the way you'd text it. This generates the actual TREC 20-19 contract -- same form, same fields, ready for review.</p>
    <div class="card">
      <form method="POST" action="/demo">
        <label class="field-label">Offer details</label>
        <input type="text" name="offer_text" placeholder="725k 3% 21day 1740 Grand Ave" value="{prefill}">
        <button type="submit">Generate offer</button>
        <div class="hint">price &middot; down % &middot; closing days &middot; address</div>
      </form>
      {result_html}
    </div>
    <div class="foot">SMS delivery pending carrier registration -- this demo runs the same backend directly</div>
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
        parsed, pdf_path, error = process_offer(offer_text, "demo-web")

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
            result_html = f"""
            <div class="result">
              <div class="result-stamp">Ready to sign</div>
              <div class="result-addr">{parsed['address']}</div>
              <div class="result-row"><span class="k">Sales price</span><span class="v">${parsed['price']:,}</span></div>
              <div class="result-row"><span class="k">Closing date</span><span class="v">{close_date_str}</span></div>
              <a href="{pdf_url}" target="_blank" class="download-btn">Download filled TREC 20-19 &rarr;</a>
              <div class="disclaimer">Draft only -- agent must review before signing. TREC NO. 20-19.</div>
            </div>
            """

    return DEMO_FORM.format(prefill=prefill, result_html=result_html, date_stamp=date_stamp)


@app.route("/offers/<path:filename>")
def serve_offer(filename):
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=False)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
