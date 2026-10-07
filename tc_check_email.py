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
# White wordmark on the dark-green header (replaced the old black icon logo
# 2026-10-07 to match the site's green/yellow redesign).
_LOGO_URL = "https://txtanoffer.com/static/logo-wordmark-white.png?v=1"
_GREEN_DARK, _GREEN, _GREEN_TINT = "#0a3f3a", "#0b5d52", "#E7F3F1"
_YELLOW, _YELLOW_TINT = "#f5c242", "#FFF6DA"
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
    "initials_buyer": "PAGES NOT INITIALED",
    "initials_seller": "PAGES NOT INITIALED",
    "loan_amount_mismatch": "FINANCING TERMS DISAGREE",
    "addendum_checkbox_mismatch": "ADDENDUM CHECKBOX WRONG",
    "amendment_price_mismatch": "PRICE TERMS DISAGREE",
    "amendment_address_mismatch": "WRONG FILE ATTACHED",
    "extra_file_unrecognized": "ATTACHMENT NOT VERIFIED",
}

_UPSELL_URL = "https://txtanoffer.com/brokers?src=report_email"


def _consequence_tag(issue: dict) -> str:
    return _CONSEQUENCE_TAGS.get(issue.get("key"), issue.get("severity", "issue").upper())


def _join_pages(labels: list) -> str:
    """['Page 1 of 12', 'Page 4 of 12', '40-11 addendum'] ->
    'pages 1 and 4 of 12, plus the 40-11 addendum'."""
    import re as _re
    nums = [m.group(1) for l in labels for m in [_re.match(r"Page (\d+) of (\d+)", l)] if m]
    totals = {m.group(2) for l in labels for m in [_re.match(r"Page (\d+) of (\d+)", l)] if m}
    others = [l for l in labels if not _re.match(r"Page \d+ of \d+", l)]
    parts = []
    if nums:
        word = "page" if len(nums) == 1 else "pages"
        joined = nums[0] if len(nums) == 1 else ", ".join(nums[:-1]) + " and " + nums[-1]
        parts.append(f"{word} {joined}" + (f" of {totals.pop()}" if len(totals) == 1 else ""))
    for o in others:
        parts.append(f"the {o}")
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + ", plus " + parts[-1]


def _display_issues(issues: list) -> list:
    """Same issues, but the per-page initials findings (up to 14 rows on a
    blank file) folded into one or two readable lines. Everything else
    passes through unchanged and in order. Counts shown in the report and
    subject come from this list, so they match what the reader sees."""
    pages = {"initials_buyer": [], "initials_seller": []}
    out, placeholder_at = [], None
    for issue in issues:
        key = issue.get("key")
        if key in pages:
            pages[key].append((issue.get("message", "").split(":")[0]).strip())
            if placeholder_at is None:
                placeholder_at = len(out)
                out.append(None)
            continue
        out.append(issue)
    if placeholder_at is not None:
        b, sl = pages["initials_buyer"], pages["initials_seller"]
        merged = []
        if b and b == sl:
            merged.append({"severity": "blocker", "key": "initials_buyer",
                           "message": f"Buyer and seller initials missing on {_join_pages(b)}"})
        else:
            if b:
                merged.append({"severity": "blocker", "key": "initials_buyer",
                               "message": f"Buyer initials missing on {_join_pages(b)}"})
            if sl:
                merged.append({"severity": "blocker", "key": "initials_seller",
                               "message": f"Seller initials missing on {_join_pages(sl)}"})
        out[placeholder_at:placeholder_at + 1] = merged
    return out


def _status_headline(result: dict) -> tuple:
    """(label, color, bg) for the top-of-email status line. Ties the
    highest-alarm label (TITLE KICKBACK RISK) to the one issue this app can
    actually back with a real, already-documented consequence; every other
    blocker case gets a still-serious but non-specific label instead of a
    made-up universal claim."""
    issues = _display_issues(result.get("issues") or [])
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


def _property_line(result: dict) -> str:
    """'123 Main St, Austin' as written in Section 2A, or '' if blank."""
    prop = result.get("property") or {}
    return ", ".join(x for x in (prop.get("address", ""), prop.get("city", "")) if x)


def subject_line(result: dict) -> str:
    if not result.get("recognized", True):
        return "TC File Check: file not recognized"
    issues = _display_issues(result.get("issues") or [])
    blockers = sum(1 for i in issues if i.get("severity") == "blocker")
    where = _property_line(result)
    prefix = f"TC File Check: {where} — " if where else "TC File Check: "
    if not issues:
        return prefix + "ready to send"
    if blockers:
        return prefix + f"{blockers} blocker{'s' if blockers != 1 else ''} found"
    n = len(issues)
    return prefix + f"{n} item{'s' if n != 1 else ''} to review"

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


_AUTOMATED_LOCAL_RE = re.compile(r"^(no-?reply|do-?not-?reply|donotreply|mailer-daemon|postmaster|bounces?)", re.I)
_OWN_DOMAIN = "txtanoffer.com"


def junk_sender_reason(sender: str, form) -> str:
    """Why this inbound message shouldn't get a reply, or "" if it should.
    Added 2026-10-02 after the intake was found replying to a stream of
    spam ("donotreplyus@inbox.lv", spoofed "@yale.edu", even its own
    tc@check.txtanoffer.com address) -- every one of those got an
    auto-reply, which is backscatter that hurts SendGrid sender reputation,
    and padded the "files checked" stats on /analytics.

    Uses SendGrid's own SPF/DKIM results ('SPF' e.g. "pass", 'dkim' e.g.
    "{@gmail.com : pass}"): a real agent forwarding from Gmail/Outlook/a
    brokerage domain passes at least one; a forged From address passes
    neither. If SendGrid sent neither field, that check is skipped rather
    than rejecting everything."""
    local, _, domain = sender.partition("@")
    spf = (form.get("SPF") or "").strip().lower()
    dkim = (form.get("dkim") or "").lower()
    auth_known = bool(spf or dkim)
    auth_ok = spf == "pass" or ": pass" in dkim
    # The intake address itself (and anything @check.) is never a real
    # forwarder -- replying would loop.
    intake = "check." + _OWN_DOMAIN
    if domain == intake or domain.endswith("." + intake):
        return "own_intake"
    # Other @txtanoffer.com senders (e.g. support@, sent via Gmail's "send
    # as") are allowed when SPF or DKIM passes. Changed 2026-10-07: this used
    # to reject the whole domain, which silently dropped the owner's own
    # test forwards; forged copies still fail authentication.
    if (domain == _OWN_DOMAIN or domain.endswith("." + _OWN_DOMAIN)) and not auth_ok:
        return "own_domain_unverified"
    if _AUTOMATED_LOCAL_RE.match(local):
        return "automated_sender"
    if auth_known and not auth_ok:
        return "auth_failed"
    return ""


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


def _clean(msg: str) -> str:
    return (msg or "").replace(" -- ", " — ")


def _files_line(result: dict) -> str:
    parts = ["TREC 20-19"]
    if result.get("has_addendum"):
        parts.append("40-11 addendum")
    if result.get("has_amendment"):
        parts.append("39-11 amendment")
    return " + ".join(parts)


def _checked_date() -> str:
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Chicago"))
    except Exception:
        now = datetime.utcnow()
    return now.strftime("%b %-d, %Y")


_ARCHIVE_TEXT = (
    "Keep every contract on file: on the Brokerage plan, forward executed "
    "contracts here from your brokerage email or drop them into your archive. "
    "Each one is checked and kept 5 years, searchable by address -- longer than "
    "the 4 years TREC requires brokers to keep transaction records (22 TAC 535.2). "
    "https://txtanoffer.com/archive"
)

_UPSELL_TEXT = (
    "Every file, every agent, checked automatically: the Brokerage plan, "
    "$349/mo for your whole roster. Start with a free audit of your last 20 "
    f"closed files: {_UPSELL_URL}"
)

_DISCLAIMER = ("TC Check flags blanks and mismatches on the fields it can verify. "
               "It isn't legal advice and doesn't replace your review. "
               "TxtAnOffer is not affiliated with TREC.")


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _streak_note(check_count: int) -> str:
    """A quiet 'this is your Nth file' line. Silent on a first-ever check
    and on an unknown count (0), so it only appears once it's true."""
    if check_count and check_count >= 2:
        return f"This is your {_ordinal(check_count)} file checked with TC Check. "
    return ""


def _issue_group_text(issues: list, heading: str) -> str:
    if not issues:
        return ""
    lines = [f"{heading} ({len(issues)}):"]
    for issue in issues:
        lines.append(f"- [{_consequence_tag(issue)}] {_clean(issue.get('message', ''))}")
    return "\n".join(lines) + "\n"


def _archived_text(archived) -> str:
    if not archived:
        return ""
    n, d = archived.get("saved", 0), archived.get("duplicates", 0)
    what = f"{n} file{'s' if n != 1 else ''} saved" if n else "Already in your archive"
    extra = f" ({d} already there)" if n and d else ""
    return (f"{what} to {archived.get('brokerage')}'s archive{extra} -- kept 5 years, searchable by address. "
            "View it: https://txtanoffer.com/broker/archive\n\n")


def format_reply_body(result: dict, check_count: int = 0, archived: dict = None) -> str:
    if not result["recognized"]:
        return (
            "We couldn't read that as a TREC 20-19 we recognize.\n\n"
            "This works with AcroForm-fillable TREC 20-19 PDFs (not scanned "
            "or flattened files). If you forwarded a scan, try re-sending "
            "the original fillable PDF instead.\n\n"
            "-- TxtAnOffer TC Check"
        )

    issues = _display_issues(result.get("issues") or [])
    blockers = [i for i in issues if i.get("severity") == "blocker"]
    warnings = [i for i in issues if i.get("severity") == "warning"]
    label, _, _ = _status_headline(result)
    prop = result.get("property") or {}
    where = _property_line(result) or "Property address not filled in (Section 2A)"
    county = f" · {prop['county']} County" if prop.get("county") else ""

    body = (f"TC FILE CHECK REPORT\n{where}{county}\n"
            f"Checked {_checked_date()} · {_files_line(result)}\n\n"
            + _archived_text(archived) +
            f"Status: {label}\n\n")
    if not issues:
        body += "Nothing to fix on the fields TC Check verifies. This one's ready.\n\n"
    else:
        body += _issue_group_text(blockers, "Fix before this goes to title") + "\n"
        body += _issue_group_text(warnings, "Worth fixing") + "\n"
        body += ("What to do next:\n"
                 "1. Fill in or correct the items above.\n"
                 "2. Check it again: forward the corrected file to tc@check.txtanoffer.com, "
                 "or upload it at txtanoffer.com/tc-check.\n"
                 "3. Share this with your agent: just forward this email.\n\n")
    body += _ARCHIVE_TEXT + "\n\n" + _UPSELL_TEXT + "\n\n"
    body += ("---\n"
             f"{_streak_note(check_count)}Check another file: tc@check.txtanoffer.com "
             "or txtanoffer.com/tc-check\n" + _DISCLAIMER)
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
# Inline styles + table layout only: the usual constraints for HTML email,
# where stylesheets and modern CSS get stripped by many clients. Redesigned
# 2026-10-07 to the site's green/yellow look, with the property address as
# the headline and every issue listed (no "...and N more" cutoff).

_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_P_STYLE = f"margin:0 0 16px;font-size:14px;line-height:1.6;color:#2c3d4d;font-family:{_FONT};"


def _issue_group_html(issues: list, heading: str) -> str:
    if not issues:
        return ""
    rows = []
    for issue in issues:
        blocker = issue.get("severity") == "blocker"
        color, bg = (_BLOCKER_COLOR, _BLOCKER_BG) if blocker else (_WARNING_COLOR, _WARNING_BG)
        rows.append(f"""
          <tr><td style="padding:11px 0;border-bottom:1px solid #eef0f2;font-family:{_FONT};">
            <div style="font-size:10px;font-weight:700;letter-spacing:0.06em;color:{color};margin-bottom:3px;">
              <span style="display:inline-block;background:{bg};border-radius:4px;padding:2px 7px;">{escape(_consequence_tag(issue))}</span>
            </div>
            <div style="font-size:14px;line-height:1.5;color:#0f1f2f;">{escape(_clean(issue.get('message', '')))}</div>
          </td></tr>""")
    return (
        f'<p style="margin:26px 0 4px;font-size:11px;font-weight:700;letter-spacing:0.07em;'
        f'text-transform:uppercase;color:{_GREEN};font-family:{_FONT};">{escape(heading)} '
        f'<span style="color:#8a9aa9;">({len(issues)})</span></p>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(rows)}</table>'
    )


def _email_shell(heading: str, subheading: str, body_html: str, kicker: str = "TC File Check") -> str:
    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#F5F5F7;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F5F5F7;padding:28px 12px;">
    <tr><td align="center">
      <table role="presentation" width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:14px;max-width:560px;width:100%;overflow:hidden;border:1px solid #e6e9ec;">
        <tr><td style="background:{_GREEN_DARK};padding:22px 32px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
            <td><img src="{_LOGO_URL}" width="150" height="25" alt="txtanoffer" style="display:block;border:0;"></td>
            <td align="right" style="font-family:{_FONT};font-size:10px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:{_YELLOW};">{escape(kicker)}</td>
          </tr></table>
        </td></tr>
        <tr><td style="background:{_YELLOW};height:4px;line-height:4px;font-size:0;">&nbsp;</td></tr>
        <tr><td style="padding:28px 32px 8px;font-family:{_FONT};">
          <h1 style="margin:0 0 6px;font-size:22px;line-height:1.25;color:#0f1f2f;font-family:{_FONT};">{heading}</h1>
          <p style="margin:0 0 22px;font-size:13px;color:#5a6b7a;font-family:{_FONT};">{subheading}</p>
          {body_html}
        </td></tr>
        <tr><td style="padding:18px 32px;background:#F5F5F7;text-align:center;">
          <a href="https://txtanoffer.com" style="font-family:{_FONT};color:{_GREEN};font-size:12px;font-weight:600;text-decoration:none;">txtanoffer.com</a>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _status_card_html(result: dict) -> str:
    label, color, bg = _status_headline(result)
    issues = _display_issues(result.get("issues") or [])
    nb = sum(1 for i in issues if i.get("severity") == "blocker")
    nw = sum(1 for i in issues if i.get("severity") == "warning")
    chips = ""
    if nb:
        chips += f'<span style="display:inline-block;margin:8px 8px 0 0;font-size:12px;font-weight:700;color:{_BLOCKER_COLOR};">{nb} must fix</span>'
    if nw:
        chips += f'<span style="display:inline-block;margin:8px 8px 0 0;font-size:12px;font-weight:700;color:{_WARNING_COLOR};">{nw} to review</span>'
    return (f'<div style="background:{bg};border-left:4px solid {color};border-radius:8px;padding:14px 16px;">'
            f'<div style="font-size:14px;font-weight:800;letter-spacing:0.02em;color:{color};text-transform:uppercase;font-family:{_FONT};">{escape(label)}</div>'
            f'<div style="font-family:{_FONT};">{chips}</div></div>')


def _next_steps_html() -> str:
    step = f"margin:0 0 8px;font-size:13px;line-height:1.55;color:#2c3d4d;font-family:{_FONT};"
    return (f'<div style="margin-top:26px;background:{_GREEN_TINT};border-radius:10px;padding:16px 18px;">'
            f'<p style="margin:0 0 10px;font-size:11px;font-weight:700;letter-spacing:0.07em;text-transform:uppercase;color:{_GREEN};font-family:{_FONT};">What to do next</p>'
            f'<p style="{step}"><strong>1.</strong> Fill in or correct the items above.</p>'
            f'<p style="{step}"><strong>2.</strong> Check it again: forward the corrected file to '
            f'<a href="mailto:tc@check.txtanoffer.com" style="color:{_GREEN};font-weight:600;">tc@check.txtanoffer.com</a> or '
            f'<a href="https://txtanoffer.com/tc-check" style="color:{_GREEN};font-weight:600;">upload it</a>.</p>'
            f'<p style="{step}margin-bottom:0;"><strong>3.</strong> Share this with your agent: just forward this email.</p></div>')


def _archive_html() -> str:
    return (f'<div style="margin-top:16px;background:{_YELLOW_TINT};border:1px solid {_YELLOW};border-radius:10px;padding:16px 18px;">'
            f'<p style="margin:0 0 6px;font-size:14px;font-weight:700;color:#0f1f2f;font-family:{_FONT};">Keep every contract on file</p>'
            f'<p style="margin:0;font-size:13px;line-height:1.55;color:#3a3320;font-family:{_FONT};">On the Brokerage plan, forward executed contracts here from your brokerage email or drop them into your archive. '
            f'Each one is checked and <strong>kept 5 years</strong>, searchable by address &mdash; longer than the 4 years TREC requires brokers to keep transaction records (22 TAC &sect;535.2). '
            f'<a href="https://txtanoffer.com/archive" style="color:{_GREEN};font-weight:600;">How it works &rarr;</a></p></div>')


def _upsell_html() -> str:
    return (f'<div style="margin-top:16px;background:{_GREEN_DARK};border-radius:10px;padding:18px 20px;">'
            f'<p style="margin:0 0 6px;font-size:15px;font-weight:700;color:#ffffff;font-family:{_FONT};">Every file, every agent, checked automatically</p>'
            f'<p style="margin:0 0 14px;font-size:13px;line-height:1.55;color:#c9dcd8;font-family:{_FONT};">The Brokerage plan checks every offer your agents send before it goes out &mdash; $349/mo for your whole roster. Start with a free audit of your last 20 closed files.</p>'
            f'<a href="{_UPSELL_URL}" style="display:inline-block;font-size:13px;font-weight:700;color:#0f1f2f;background:{_YELLOW};padding:10px 18px;border-radius:999px;text-decoration:none;font-family:{_FONT};">Get a free 20-file audit &rarr;</a></div>')


def _footer_html(check_count: int = 0) -> str:
    streak = _streak_note(check_count)
    streak_html = f"{escape(streak)}<br>" if streak else ""
    return (f'<p style="margin:24px 0 0;padding-top:16px;border-top:1px solid #eef0f2;font-size:12px;line-height:1.6;color:#8a9aa9;font-family:{_FONT};">'
            f'{streak_html}Check another file: <a href="mailto:tc@check.txtanoffer.com" style="color:{_GREEN};">tc@check.txtanoffer.com</a> or '
            f'<a href="https://txtanoffer.com/tc-check" style="color:{_GREEN};">txtanoffer.com/tc-check</a><br>'
            f'<span style="font-size:11px;">{escape(_DISCLAIMER)}</span></p>')


def _archived_html(archived) -> str:
    if not archived:
        return ""
    n, d = archived.get("saved", 0), archived.get("duplicates", 0)
    what = f"{n} file{'s' if n != 1 else ''} saved" if n else "Already in your archive"
    extra = f" ({d} already there)" if n and d else ""
    return (f'<div style="margin-bottom:14px;background:{_GREEN_TINT};border-radius:8px;padding:12px 16px;font-family:{_FONT};font-size:13px;color:#0f1f2f;">'
            f'&#10003; <strong>{escape(what)} to {escape(archived.get("brokerage", "your brokerage"))}&rsquo;s archive</strong>{escape(extra)} &mdash; kept 5 years, searchable by address. '
            f'<a href="https://txtanoffer.com/broker/archive" style="color:{_GREEN};font-weight:600;">Open the archive &rarr;</a></div>')


def format_reply_html(result: dict, check_count: int = 0, archived: dict = None) -> str:
    if not result["recognized"]:
        body = (
            f'<p style="{_P_STYLE}">This works with AcroForm-fillable TREC 20-19 PDFs '
            f"(not scanned or flattened files). If you forwarded a scan, try re-sending "
            f"the original fillable PDF instead.</p>"
        )
        return _email_shell("We couldn&rsquo;t read that file", "It didn&rsquo;t match a TREC 20-19 we recognize", body)

    issues = _display_issues(result.get("issues") or [])
    blockers = [i for i in issues if i.get("severity") == "blocker"]
    warnings = [i for i in issues if i.get("severity") == "warning"]
    prop = result.get("property") or {}

    if prop.get("address"):
        heading = escape(prop["address"])
        sub_bits = [escape(x) for x in (prop.get("city", ""), f"{prop['county']} County" if prop.get("county") else "") if x]
    else:
        heading = '<span style="color:#b91c1c;">Property address not filled in</span>'
        sub_bits = ["Section 2A is blank"]
    sub_bits.append(f"Checked {_checked_date()}")
    sub_bits.append(escape(_files_line(result)))
    subheading = " &middot; ".join(sub_bits)

    body = _archived_html(archived) + _status_card_html(result)
    if not issues:
        body += f'<p style="{_P_STYLE}margin-top:18px;color:#15803d;">Nothing to fix on the fields TC Check verifies &mdash; this one&rsquo;s ready.</p>'
    else:
        body += _issue_group_html(blockers, "Fix before this goes to title")
        body += _issue_group_html(warnings, "Worth fixing")
        body += _next_steps_html()
    body += _archive_html()
    body += _upsell_html()
    body += _footer_html(check_count)
    return _email_shell(heading, subheading, body, kicker="TC File Check report")


def format_no_pdf_html() -> str:
    body = (
        f'<p style="{_P_STYLE}">Forward the TREC 20-19 as a PDF attachment (not a scanned '
        f"image or a link) and we'll reply with an itemized check of what's missing.</p>"
    )
    return _email_shell("No PDF found", "We didn&rsquo;t find a PDF attached to that email", body)


def format_unreadable_html() -> str:
    body = f'<p style="{_P_STYLE}">Make sure it\'s not corrupted or password-protected, and try forwarding again.</p>'
    return _email_shell("Couldn&rsquo;t read that file", "It didn&rsquo;t open as a valid PDF", body)
