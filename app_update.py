# Add this route after the /demo route in app.py

@app.route("/robots.txt")
def robots_txt():
    return send_from_directory(".", "robots.txt", mimetype="text/plain")

# Update the footer consent language in DEMO_FORM to include explicit SMS opt-in:
# Replace this line in the .foot section:
# "By texting or using this service, you consent to receive SMS responses. Reply STOP to opt out anytime. Msg & data rates may apply."
# With this (already in the provided code above):
# "By texting or using this service, you consent to receive SMS responses. Reply STOP to opt out anytime. Msg & data rates may apply."

