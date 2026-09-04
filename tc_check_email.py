"""
tc_check_email.py -- Email-forward intake for TC File Check (see tc_audit.py).

Phase 1 of the "document-driven" TC Check pitch: instead of an agent/TC
uploading a PDF to txtanoffer.com, they forward the TREC 20-19 (and,
optionally, its 40-11 addendum) to a dedicated inbox and get the same
itemized report back by reply. This module only handles the SendGrid
Inbound Parse payload -> PDF attachments -> reply-text plumbing; the actual
audit logic is unchanged and lives entirely in tc_audit.check_tc_file().

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

MAX_ATTACHMENTS = 2  # contract + optional 40-11 addendum, same cap check_tc_file() expects

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


def format_reply_body(result: dict) -> str:
    from tc_nudge import issue_summary

    if not result["recognized"]:
        return (
            "We couldn't read that as a TREC 20-19 we recognize.\n\n"
            "This works with AcroForm-fillable TREC 20-19 PDFs (not scanned "
            "or flattened files). If you forwarded a scan, try re-sending "
            "the original fillable PDF instead.\n\n"
            "-- TxtAnOffer TC Check"
        )
    return (
        "Here's your TC File Check on the file you forwarded.\n\n"
        f"{issue_summary(result)}\n\n"
        + ("Nothing to fix -- this one's ready.\n\n" if result["complete"] else "")
        + "Forward another file any time to check it too.\n\n"
        "-- TxtAnOffer TC Check"
    )


def format_no_pdf_reply() -> str:
    return (
        "We didn't find a PDF attached to that email.\n\n"
        "Forward the TREC 20-19 as a PDF attachment (not a scanned image or "
        "a link) and we'll reply with an itemized check of what's missing.\n\n"
        "-- TxtAnOffer TC Check"
    )
