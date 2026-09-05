"""
tc_check_email.py -- Email-forward intake for TC File Check (see tc_audit.py).

Phase 1 of the "document-driven" TC Check pitch: instead of an agent/TC
uploading a PDF to txtanoffer.com, they forward the TREC 20-19 (and,
optionally, its 40-11 addendum and/or 39-11 amendment) to a dedicated inbox
and get the same itemized report back by reply. This module only handles
the SendGrid Inbound Parse payload -> PDF attachments -> reply-text
plumbing; the actual audit logic is unchanged and lives entirely in
tc_audit.check_tc_file().

Why a separate module instead of inline in app.py: app.py is already large
and every other TC Check concern (tc_audit, tc_gate, tc_nudge) is already
split out the same way -- this keeps SendGrid's payload-shape and email-
address parsing out of the route handler.

SendGrid Inbound Parse posts multipart/form-data with (among other fields):
  from, to, subject, text, html, attachments (count),
  attachment1..attachmentN (files), attachment-info (JSON metadata)
Docs: https://www.twilio.com/docs/sendgrid/for-developers/parsing-email/setting-up-the-inbound-parse-webhook
"""
import re
from html import escape

MAX_ATTACHMENTS = 3  # contract + optional 40-11 addendum + optional 39-11 amendment, same cap check_tc_file() expects

# Same brand colors as the web TC Check page's .issue-tag.blocker/.warning
# (see app.py) -- kept in sync by eye, not shared code, since one lives in
# a CSS block and the other in inline-styled HTML email markup.
_LOGO_URL = "https://txtanoffer.com/static/logo.png"
_BLOCKER_COLOR, _BLOCKER_BG = "#dc2626", "rgba(239,68,68,0.10)"
_WARNING_COLOR, _WARNING_BG = "#b45309", "rgba(245,158,11,0.10)"
_CLEAR_COLOR, _CLEAR_BG = "#15803d", "rgba(21,128,61,0.10)"

# Per-field consequence tags for the email report -- what actually happens
# downstream if this specific field stays blank, not just "blocker" or
# "warning". Deliberately conservative: every tag here describes something
# this app's own logic already treats as true (a field pdf_validator.py or
# tc_audit.py already gates on) or a plain fact about what the field does
# on the form itself. Nothing here asserts a legal conclusion (e.g. "this
# makes the contract void") that would need a licensed opinion to back --
# see tc_audit.py's own comment on why buyer/seller name is a warning, not
# a blocker: this app never collects those fields either, so a Blocker tag
# on them would be inaccurate, not just uncharitable. Keys not listed here
# fall back to their plain severity in _consequence_tag().
_CONSEQUENCE_TAGS = {
    "address": "PROPERTY NOT IDENTIFIED",
    "city": "PROPERTY NOT IDENTIFIED",
    "county": "TITLE KICKBACK",  # tc_audit.py's own CHECKED_FIELDS message: "title will kick back the file without this"
    "buyer_name": "PARTY NOT NAMED",
    "seller_name": "PARTY NOT NAMED",
    "escrow_agent_name": "NO ESCROW AGENT ON FILE",
    "earnest_money_amount": "DEAL TERMS INCOMPLETE",
    "option_fee_amount": "DEAL TERMS INCOMPLETE",
    "title_company": "NO TITLE COMPANY NAMED",
    "effective_date": "DEADLINES UNANCHORED",  # option/financing/closing dates all run off this one field
    "initials_buyer": "PAGE NOT INITIALED",
    "initials_seller": "PAGE NOT INITIALED",
    "loan_amount_mismatch": "FINANCING TERMS DISAGREE",
    "addendum_checkbox_mismatch": "ADDENDUM CHECKBOX WRONG",
    "amendment_price_mismatch": "PRICE TERMS DISAGREE",
    "amendment_address_mismatch": "WRONG FILE ATTACHED",
    "extra_file_unrecognized": "ATTACHMENT NOT VERIFIED",
}

_UPSELL_URL = "https://txtanoffer.com/pricing#brokerage"


def _consequence_tag(issue: dict) -> str:
    return _CONSEQUENCE_TAGS.get(issue.get("key"), issue.get("severity", "issue").upper())


def _status_headline(result: dict) -> tuple:
    """(label, color, bg) for the top-of-email status line. Ties the
    highest-alarm label (TITLE KICKBACK RISK) to the one issue this app can
    actually back with a real, already-documented consequence; every other
    blocker case gets a still-serious but non-specific label instead of a
    made-up universal claim."""
    issues = result.get("issues") or []
    blockers = [i for i in issues if i.get("severity") == "blocker"]
    if not issues:
        return ("CLEAR — Ready to send", _CLEAR_COLOR, _CLEAR_BG)
    if blockers:
        if any(i.get("key") == "county" for i in blockers):
            return ("TITLE KICKBACK RISK", _BLOCKER_COLOR, _BLOCKER_BG)
        n = len(blockers)
        return (f"NOT READY — {n} item{'s' if n != 1 else ''} will stop this deal", _BLOCKER_COLOR, _BLOCKER_BG)
    n = len(issues)
    return (f"{n} item{'s' if n != 1 else ''} to review", _WARNING_COLOR, _WARNING_BG)


def subject_line(result: dict) -> str:
    if not result.get("recognized", True):
        return "TC File Check: file not recognized"
    issues = result.get("issues") or []
    blockers = sum(1 for i in issues if i.get("severity") == "blocker")
    if not issues:
        return "TC File Check: ready to send"
    if blockers:
        return f"TC File Check: {blockers} blocker{'s' if blockers != 1 else ''} found — action needed"
    n = len(issues)
    return f"TC File Check: {n} item{'s' if n != 1 else ''} to review"

# RFC 5322 is a much bigger grammar than this, but every real mail client
# sends "From" as either a bare address or "Display Name <addr>" -- this
# only needs to handle those two shapes, not validate arbitrary input.
_EMAIL_RE = re.compile(r"[^\s<>\"]+@[^\s<>\"]+\.[^\s<>\"]+")


def extract_sender_email(from_field: str) -> str:
    """'"Jane TC" <jane@brokerage.com>' -> 'jane@brokerage.com'. Returns ""
    if nothing address-shaped is found."""
    if not from_field:
        return ""
    match = _EMAIL_RE.search(from_field)
    return match.group(0).lower() if match else ""


def extract_pdf_attachments(files, form) -> list:
    """files: request.files (werkzeug MultiDict), form: request.form.
    Returns up to MAX_ATTACHMENTS werkzeug FileStorage objects whose
    filename ends in .pdf, in SendGrid's attachmentN order. Non-PDF
    attachments (a signature image, a logo in an email footer, etc.) are
    silently skipped rather than rejecting the whole message -- the
    realistic case is a forwarded email with the contract PDF plus other
    junk attached, not a deliberately malformed upload."""
    try:
        count = int(form.get("attachments", 0))
    except (TypeError, ValueError):
        count = 0

    pdfs = []
    for i in range(1, count + 1):
        f = files.get(f"attachment{i}")
        if f and f.filename and f.filename.lower().endswith(".pdf"):
            pdfs.append(f)
        if len(pdfs) >= MAX_ATTACHMENTS:
            break
    return pdfs


_UPSELL_TEXT = (
    "\nTired of catching these by hand?\n"
    "The TxtAnOffer Brokerage Dashboard checks every agent's file "
    "automatically, before it's ever sent -- $399/mo for your whole roster.\n"
    f"See how it works: {_UPSELL_URL}\n"
)


def _issue_group_text(issues: list, heading: str, limit: int = 6) -> str:
    if not issues:
        return ""
    lines = [f"{heading}:"]
    for issue in issues[:limit]:
        lines.append(f"- [{_consequence_tag(issue)}] {issue.get('message', '')}")
    if len(issues) > limit:
        lines.append(f"...and {len(issues) - limit} more.")
    return "\n".join(lines) + "\n"


def format_reply_body(result: dict) -> str:
    if not result["recognized"]:
        return (
            "We couldn't read that as a TREC 20-19 we recognize.\n\n"
            "This works with AcroForm-fillable TREC 20-19 PDFs (not scanned "
            "or flattened files). If you forwarded a scan, try re-sending "
            "the original fillable PDF instead.\n\n"
            "-- TxtAnOffer TC Check"
        )

    issues = result.get("issues") or []
    blockers = [i for i in issues if i.get("severity") == "blocker"]
    warnings = [i for i in issues if i.get("severity") == "warning"]
    label, _, _ = _status_headline(result)

    body = f"Status: {label}\n\n"
    if not issues:
        body += "Nothing to fix -- this one's ready.\n\n"
    else:
        body += _issue_group_text(blockers, "Critical deal blockers") + "\n"
        body += _issue_group_text(warnings, "Also worth fixing") + "\n"
        body += _UPSELL_TEXT + "\n"
    body += "Forward another file any time to check it too.\n\n-- TxtAnOffer TC Check"
    return body


def format_no_pdf_reply() -> str:
    return (
        "We didn't find a PDF attached to that email.\n\n"
        "Forward the TREC 20-19 as a PDF attachment (not a scanned image or "
        "a link) and we'll reply with an itemized check of what's missing.\n\n"
        "-- TxtAnOffer TC Check"
    )


def format_unreadable_reply() -> str:
    return (
        "Couldn't read that as a PDF -- make sure it's not corrupted or "
        "password-protected, and try forwarding again.\n\n"
        "-- TxtAnOffer TC Check"
    )


# --- HTML counterparts -------------------------------------------------
# SendGrid requires text/plain to accompany text/html (see
# integrations.send_html_email) -- these render the same content the
# _reply functions above already produce as plain text, just with the
# logo and issue-severity badges a plain-text email can't have. Inline
# styles only, table-based layout: the usual constraints for HTML email,
# where external stylesheets and modern CSS often get stripped by the
# receiving client.

_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _issue_group_html(issues: list, heading: str, limit: int = 6) -> str:
    if not issues:
        return ""
    rows = []
    for issue in issues[:limit]:
        severity = issue.get("severity", "issue")
        color, bg = (_BLOCKER_COLOR, _BLOCKER_BG) if severity == "blocker" else (_WARNING_COLOR, _WARNING_BG)
        tag = _consequence_tag(issue)
        rows.append(f"""
          <tr>
            <td style="padding:9px 0;border-bottom:1px solid #f0f0ee;font-family:{_FONT};">
              <span style="display:inline-block;font-size:11px;font-weight:700;letter-spacing:0.03em;color:{color};background:{bg};border-radius:4px;padding:2px 8px;margin-right:8px;white-space:nowrap;">{escape(tag)}</span>
              <span style="font-size:14px;color:#171717;">{escape(issue.get('message', ''))}</span>
            </td>
          </tr>""")
    if len(issues) > limit:
        rows.append(f"""
          <tr><td style="padding:9px 0;font-size:13px;color:#737373;font-family:{_FONT};">
            &hellip;and {len(issues) - limit} more.
          </td></tr>""")
    return (
        f'<p style="margin:20px 0 8px;font-size:11px;font-weight:700;letter-spacing:0.05em;'
        f'text-transform:uppercase;color:#737373;font-family:{_FONT};">{escape(heading)}</p>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(rows)}</table>'
    )


_UPSELL_HTML = f"""
<div style="margin-top:24px;padding:16px 20px;background:#171717;border-radius:8px;">
  <p style="margin:0 0 6px;font-size:13px;font-weight:600;color:#ffffff;font-family:{_FONT};">Tired of catching these by hand?</p>
  <p style="margin:0 0 14px;font-size:13px;line-height:1.5;color:#a3a3a3;font-family:{_FONT};">The TxtAnOffer Brokerage Dashboard checks every agent's file automatically, before it's ever sent &mdash; $399/mo for your whole roster.</p>
  <a href="{_UPSELL_URL}" style="display:inline-block;font-size:13px;font-weight:600;color:#171717;background:#ffffff;padding:8px 16px;border-radius:6px;text-decoration:none;font-family:{_FONT};">See how it works &rarr;</a>
</div>
"""


def _email_shell(heading: str, subheading: str, body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#F0F0EE;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F0F0EE;padding:32px 16px;">
    <tr><td align="center">
      <table role="presentation" width="480" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;max-width:480px;width:100%;">
        <tr><td style="padding:24px 32px;border-bottom:1px solid #eeeeee;">
          <img src="{_LOGO_URL}" width="32" height="32" alt="TxtAnOffer" style="border-radius:22%;display:block;">
        </td></tr>
        <tr><td style="padding:28px 32px 8px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
          <h1 style="margin:0 0 4px;font-size:18px;color:#171717;">{escape(heading)}</h1>
          <p style="margin:0 0 20px;font-size:13px;color:#737373;">{escape(subheading)}</p>
          {body_html}
        </td></tr>
        <tr><td style="padding:18px 32px;background:#F0F0EE;border-radius:0 0 12px 12px;text-align:center;">
          <a href="https://txtanoffer.com" style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#525252;font-size:12px;text-decoration:none;">txtanoffer.com</a>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


_P_STYLE = "margin:0 0 16px;font-size:14px;line-height:1.6;color:#404040;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"


def _status_banner_html(result: dict) -> str:
    label, color, bg = _status_headline(result)
    return (
        f'<div style="background:{bg};border-radius:8px;padding:12px 16px;margin-bottom:20px;">'
        f'<span style="font-size:13px;font-weight:700;letter-spacing:0.02em;color:{color};'
        f'text-transform:uppercase;font-family:{_FONT};">{escape(label)}</span></div>'
    )


def format_reply_html(result: dict) -> str:
    if not result["recognized"]:
        body = (
            f'<p style="{_P_STYLE}">This works with AcroForm-fillable TREC 20-19 PDFs '
            f"(not scanned or flattened files). If you forwarded a scan, try re-sending "
            f"the original fillable PDF instead.</p>"
        )
        return _email_shell("We couldn't read that file", "It didn't match a TREC 20-19 we recognize", body)

    issues = result.get("issues") or []
    blockers = [i for i in issues if i.get("severity") == "blocker"]
    warnings = [i for i in issues if i.get("severity") == "warning"]

    body = _status_banner_html(result)
    if not issues:
        body += f'<p style="{_P_STYLE}color:#15803d;">Nothing to fix &mdash; this one\'s ready.</p>'
    else:
        body += _issue_group_html(blockers, "Critical deal blockers")
        body += _issue_group_html(warnings, "Also worth fixing")
        body += _UPSELL_HTML
    body += f'<p style="{_P_STYLE}margin-top:20px;">Forward another file any time to check it too.</p>'
    return _email_shell("TC File Check results", "On the file you forwarded", body)


def format_no_pdf_html() -> str:
    body = (
        f'<p style="{_P_STYLE}">Forward the TREC 20-19 as a PDF attachment (not a scanned '
        f"image or a link) and we'll reply with an itemized check of what's missing.</p>"
    )
    return _email_shell("No PDF found", "We didn't find a PDF attached to that email", body)


def format_unreadable_html() -> str:
    body = f'<p style="{_P_STYLE}">Make sure it\'s not corrupted or password-protected, and try forwarding again.</p>'
    return _email_shell("Couldn't read that file", "It didn't open as a valid PDF", body)
