"""Emails Phanel the moment an outside sender checks a real, filled TREC
20-19 through TC Check (web upload at /v1/tc/check or email forward to
tc@check.txtanoffer.com).

Why: as of 2026-10-09 there have been 0 real outside uploads, and the week's
whole objective is landing the first one. /analytics needs a password and a
manual look, so a real file could sit unseen for a day -- long enough for
the follow-up window ("saw you ran a file, want me to walk through it?") to
go cold. This turns that event into an email within seconds.

What does NOT alert (all of it noise for that purpose):
- demo/sample runs (is_demo -- the "Run a sample check" button)
- Phanel's own traffic: the ta_internal cookie (see analytics.py's
  _is_internal_traffic) on web, and his own addresses on either path
- unrecognized files (not a 20-19 AcroForm) and blank drafts
  (looks_like_blank_draft, or the Section 2A address left empty) -- a blank
  template someone downloaded to poke at the tool is not a closed file

The alert carries only metadata (source, src tag, recognized, issue count,
sender) -- no property address or other contract contents, so Phanel's
inbox never holds a copy of what the site says is discarded. The file
itself is never attached or stored here.
"""
import hashlib
import threading

from integrations import send_plain_email

ALERT_TO = "support@txtanoffer.com"

# SHA-256 of Phanel's own addresses (lowercased), not the addresses
# themselves: this repo is public, and a raw personal address in source is
# an invitation to scrapers. Same exclusion, nothing readable to harvest.
_INTERNAL_EMAIL_HASHES = {
    "8a76a05bec63b335ec1e7648eb8bf6e7ef189e15a01051acbd231b7225c073e1",
    "d15834568c6bda3bc9ffa75b64620f732fd05bf315a08898680abf272db192ca",
}


def is_internal_email(email: str) -> bool:
    e = (email or "").strip().lower()
    return bool(e) and hashlib.sha256(e.encode("utf-8")).hexdigest() in _INTERNAL_EMAIL_HASHES


def should_alert(result: dict, sender: str = "", is_demo: bool = False, internal: bool = False) -> bool:
    if is_demo or internal or is_internal_email(sender):
        return False
    if not result or not result.get("recognized") or result.get("looks_like_blank_draft"):
        return False
    # Core field: a closed file always names the property. A form with
    # most fields filled but no address is still a template/test, not a deal.
    if not ((result.get("property") or {}).get("address") or "").strip():
        return False
    return True


def format_alert(result: dict, source: str, src_tag: str = "", sender: str = "") -> tuple:
    prop = result.get("property") or {}
    issues = result.get("issues") or []
    blockers = sum(1 for i in issues if i.get("severity") == "blocker")
    subject = f"Real TC Check upload ({source}): {sender or 'unknown sender'}"
    lines = [
        f"An outside sender just checked a filled {result.get('form') or 'TREC 20-19'}.",
        "",
        f"Source: {source}",
        f"src tag: {src_tag or 'direct'}",
        f"Sender: {sender or 'unknown (no email given)'}",
        f"Recognized: {'yes' if result.get('recognized') else 'no'}",
        f"Issues: {len(issues)} ({blockers} must-fix)",
    ]
    # 123 Main St is the sample/demo address on every TxtAnOffer asset, so
    # this is most likely one of our own files run without the demo flag.
    # Still sent (a missed real upload costs more than one extra email),
    # just labeled.
    if "123 main" in (prop.get("address") or "").lower():
        lines += ["", "Note: 123 Main St is our sample/demo address -- probably our own file, not theirs."]
    lines += ["", "The file was not kept or attached. See /analytics for the full event."]
    return subject, "\n".join(lines)


def _queue(subject: str, body: str) -> None:
    def _send():
        try:
            send_plain_email(ALERT_TO, subject, body)
        except Exception as e:
            print(f"[real_upload_alert] send failed: {e}")

    threading.Thread(target=_send, daemon=True).start()


def maybe_send_alert(result: dict, source: str, src_tag: str = "", sender: str = "",
                     is_demo: bool = False, internal: bool = False) -> bool:
    """Sends the alert on a background thread (never delays the visitor's
    response) when should_alert() passes. Returns whether one was queued."""
    if not should_alert(result, sender, is_demo, internal):
        return False
    _queue(*format_alert(result, source, src_tag, sender))
    return True


# --- Unreadable files (S1, 2026-10-24) ---------------------------------
# should_alert() above skips unrecognized files on purpose (a blank or
# random PDF isn't a closed file). But the likeliest first real file is a
# flattened Dotloop/Authentisign export, which comes back unreadable -- and
# then nobody ever saw it. This is a separate, clearly-labeled alert so
# Phanel can follow up on the hand check. Same exclusions as above (demo,
# his own traffic and addresses); the web route also counts
# source_page=claude_verify as internal. Metadata only, no file.

def should_alert_unreadable(result: dict, sender: str = "", is_demo: bool = False, internal: bool = False) -> bool:
    if is_demo or internal or is_internal_email(sender):
        return False
    return bool(result) and not result.get("recognized")


def format_unreadable_alert(source: str, src_tag: str = "", sender: str = "", hand_review_on: bool = False) -> tuple:
    subject = f"Unreadable TC Check file ({source}): {sender or 'unknown sender'}"
    lines = [
        "An outside sender's file couldn't be read automatically (flattened, scanned, or not a 20-19/CBS1).",
        "",
        f"Source: {source}",
        f"src tag: {src_tag or 'direct'}",
        f"Sender: {sender or 'unknown (no email given)'}",
        "",
        ("They were offered a hand check. If they send the file, it arrives at HAND_REVIEW_EMAIL."
         if hand_review_on else
         "HAND_REVIEW_EMAIL isn't set, so they got the old 're-send a fillable PDF' reply, with no hand-check offer."),
        "",
        "The file was not kept or attached. See /analytics for the full event.",
    ]
    return subject, "\n".join(lines)


# A TREC form that was read but has no checks yet (9-18, 25-17, an outdated
# 20-18 -- tc_audit._form_gate) is also "not recognized", but "couldn't be
# read" would be wrong. Same exclusions; its own wording. Worth seeing: it's
# the demand signal for which form to check next.
def format_not_checked_alert(result: dict, source: str, src_tag: str = "", sender: str = "") -> tuple:
    subject = f"TC Check: {result.get('form') or 'TREC form'} recognized, not checked ({source}): {sender or 'unknown sender'}"
    lines = [
        "An outside sender's file was read, but TC Check has no checks for that form (or it's outdated).",
        "",
        f"What they saw: {result['issues'][0]['message']}",
        "",
        f"Source: {source}",
        f"src tag: {src_tag or 'direct'}",
        f"Sender: {sender or 'unknown (no email given)'}",
        "",
        "No hand-check offer was shown. The file was not kept or attached.",
    ]
    return subject, "\n".join(lines)


def maybe_send_unreadable_alert(result: dict, source: str, src_tag: str = "", sender: str = "",
                                is_demo: bool = False, internal: bool = False, hand_review_on: bool = False) -> bool:
    if not should_alert_unreadable(result, sender, is_demo, internal):
        return False
    if result.get("unsupported"):
        _queue(*format_not_checked_alert(result, source, src_tag, sender))
    else:
        _queue(*format_unreadable_alert(source, src_tag, sender, hand_review_on))
    return True
