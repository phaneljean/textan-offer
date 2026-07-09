"""
app.py — Twilio SMS webhook for TextAnOffer, plus a /demo web form that
bypasses SMS entirely (for testing while A2P 10DLC registration is pending).
"""

from flask import Flask, request, send_from_directory, Response
from twilio.twiml.messaging_response import MessagingResponse
from datetime import datetime
import os

from parser import parse_offer_sms
from pdf_filler import fill_offer_pdf, OUTPUT_DIR

app = Flask(__name__)


def lookup_mls(address: str) -> dict:
    return {
        "bed": 3,
        "bath": 2,
        "sqft": 1450,
        "apn": "714-123-45",
    }


def process_offer(incoming_msg: str, source_id: str):
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
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>TextAnOffer -- Demo</title>
  <style>
    body {{ font-family: -apple-system, sans-serif; max-width: 480px; margin: 60px auto; padding: 0 20px; color: #211F1B; }}
    h1 {{ font-size: 22px; }}
    input {{ width: 100%; padding: 12px; font-size: 15px; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; margin-bottom: 12px; }}
    button {{ background: #211F1B; color: white; border: none; padding: 12px 20px; border-radius: 6px; font-size: 15px; cursor: pointer; }}
    .result {{ margin-top: 24px; padding: 16px; background: #F7F4EE; border-radius: 6px; }}
    .error {{ color: #a33; }}
    a {{ color: #A9834C; }}
  </style>
</head>
<body>
  <h1>TextAnOffer -- Demo</h1>
  <p>Same logic as the SMS webhook, no texting required.</p>
  <form method="POST" action="/demo">
    <input name="offer_text" placeholder="725k 3% 21day 1740 Grand Ave"
           value="{prefill}" required>
    <button type="submit">Generate Offer</button>
  </form>
  {result_html}
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
        parsed, pdf_path, error = process_offer(offer_text, "demo-web")

        if error:
            result_html = f'<div class="result error">{error}</div>'
        else:
            filename = os.path.basename(pdf_path)
            pdf_url = f"/offers/{filename}"
            result_html = f"""
            <div class="result">
              <strong>Offer ready for {parsed['address']}</strong><br>
              Price: ${parsed['price']:,}<br>
              Close: {parsed['close_days']} days<br><br>
              <a href="{pdf_url}" target="_blank">Download filled TREC 20-19 PDF</a><br>
              <em>Draft -- agent must review before signing.</em>
            </div>
            """

    return DEMO_FORM.format(prefill=prefill, result_html=result_html)


@app.route("/offers/<path:filename>")
def serve_offer(filename):
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=False)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
# force rebuild
# force rebuild
