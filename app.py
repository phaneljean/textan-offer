@@
 from integrations import send_offer_email, fire_webhook, save_webhook, get_webhook, delete_webhook, send_to_docusign
 from offers_db import record_offer, get_offers_for_phone, get_offer_by_filename
+from sms_utils import parse_incoming_sms
@@
 @app.route("/demo", methods=["GET", "POST"])
 def demo():
@@
     if request.method == "POST":
-        # Twilio sends inbound SMS as form-encoded POST params
-        data = request.values.to_dict()
-        print(f"[SMS] Raw webhook: {data}")
-        incoming_msg = request.values.get("Body", "")
-        agent_phone = request.values.get("From", "")
+        # Parse incoming SMS using sms_utils.parse_incoming_sms (works for Twilio webhooks)
+        result = parse_incoming_sms()
+        if not isinstance(result, tuple) or len(result) != 3:
+            # parse_incoming_sms returns a Flask response on failure (e.g., 403 signature error)
+            return result
+        form, incoming_msg, agent_phone = result
@@
         parsed = parse_offer_sms(incoming_msg)
