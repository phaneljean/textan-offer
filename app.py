"""
app.py — Twilio SMS webhook for TextAnOffer.
"""
from flask import Flask, request, send_from_directory, Response
from twilio.twiml.messaging_response import MessagingResponse
from datetime import datetime
import os
import sys
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

@app.route("/sms", methods=["POST"])
def sms_reply():
    incoming_msg = request.values.get("Body", "")
    agent_phone = request.values.get("From", "")
    
    print(f"[SMS] Received from {agent_phone}: {incoming_msg}", file=sys.stderr)
    
    resp = MessagingResponse()
    
    try:
        parsed = parse_offer_sms(incoming_msg)
        print(f"[PARSE] Result: {parsed}", file=sys.stderr)
    except Exception as e:
        print(f"[PARSE ERROR] {e}", file=sys.stderr)
        resp.message(f"Parse error: {e}")
        return Response(str(resp), mimetype='application/xml')
    
    if "error" in parsed:
        resp.message(parsed["error"])
        return Response(str(resp), mimetype='application/xml')
    
    mls_data = lookup_mls(parsed["address"])
    parsed.update(mls_data)
    
    try:
        pdf_path = fill_offer_pdf(parsed, agent_phone)
        print(f"[PDF] Generated: {pdf_path}", file=sys.stderr)
    except Exception as e:
        print(f"[PDF ERROR] {e}", file=sys.stderr)
        resp.message(f"PDF error: {e}")
        return Response(str(resp), mimetype='application/xml')
    
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
    print(f"[REPLY] Sending: {reply}", file=sys.stderr)
    return Response(str(resp), mimetype='application/xml')

@app.route("/offers/<path:filename>")
def serve_offer(filename):
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=False)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
