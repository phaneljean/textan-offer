"""
sms_utils.py — Twilio inbound SMS helper for app.py

Provides parse_incoming_sms() which:
- reads form-encoded POST payload from Twilio
- validates X-Twilio-Signature if TWILIO_AUTH_TOKEN set
- returns (form_dict, incoming_msg, agent_phone) on success
- on signature failure returns a Flask response (403)

To use: from sms_utils import parse_incoming_sms
Call inside your /sms route and proceed with the returned values.
"""

from flask import request, abort, jsonify, current_app as app
import os

try:
    from twilio.request_validator import RequestValidator
except Exception:
    RequestValidator = None

TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")


def _get_twilio_signature():
    return request.headers.get("X-Twilio-Signature", "")


def parse_incoming_sms():
    """Parse and validate an inbound Twilio SMS webhook.

    Returns: (form_dict, incoming_msg, agent_phone)
    Or returns a Flask response (e.g., (jsonify(...), 403)) on failure.
    """
    # Twilio posts form-encoded data (application/x-www-form-urlencoded)
    if request.method != "POST":
        abort(405)

    form = request.form.to_dict()
    incoming_msg = (form.get("Body") or "").strip()
    agent_phone = form.get("From", "")

    # Validate Twilio signature if token configured and RequestValidator available
    if TWILIO_AUTH_TOKEN and RequestValidator is not None:
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        full_url = request.url
        signature = _get_twilio_signature()
        try:
            valid = validator.validate(full_url, form, signature)
        except Exception:
            valid = False
        if not valid:
            app.logger.warning("Twilio signature validation failed for From=%s", agent_phone)
            return jsonify({"error": "invalid Twilio signature"}), 403
    else:
        if not TWILIO_AUTH_TOKEN:
            app.logger.warning("TWILIO_AUTH_TOKEN not set — skipping Twilio signature validation (dev only)")
        elif RequestValidator is None:
            app.logger.warning("twilio library not installed — RequestValidator unavailable")

    # Safe logging: redact phone and truncate body
    redacted_phone = ("*" * max(0, len(agent_phone) - 4)) + agent_phone[-4:] if agent_phone else ""
    app.logger.info("[SMS] From=%s Body=%s", redacted_phone, incoming_msg[:200])

    return form, incoming_msg, agent_phone
