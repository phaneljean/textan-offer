"""
tc_nudge.py -- Turns a TC File Check email capture (tc_gate.py) into a
two-touch nudge toward the paid SMS product, instead of just sitting in
the events table unused:

  1. Immediate: sent right after the gate captures an email, referencing
     the actual issues just found in their file -- the moment the pain
     ("this file has problems") is freshest.
  2. Day-3 follow-up: a lighter re-engagement touch for anyone who hasn't
     come back, sent once per client.

Both are fire-and-forget (daemon thread / best-effort) -- a SendGrid
hiccup should never affect the TC File Check response itself. The day-3
scan piggybacks on request traffic the same way reminders.py and
cleanup.py do, rather than needing a separate Railway cron.
"""
import os
import time
import threading

from integrations import send_plain_email
from tc_gate import clients_needing_followup, mark_followup_sent

FOLLOWUP_DAYS = int(os.environ.get("TC_NUDGE_FOLLOWUP_DAYS", 3))
_STATE_FILE = os.environ.get("TC_NUDGE_STATE_FILE", ".last_tc_nudge_check")
_CHECK_INTERVAL_SECONDS = 6 * 3600

PRICING_URL = "https://txtanoffer.com/pricing"


def issue_summary(result: dict) -> str:
    issues = result.get("issues") or []
    if not issues:
        return "Good news -- no issues found on this one."
    blockers = sum(1 for i in issues if i.get("severity") == "blocker")
    warnings = len(issues) - blockers
    parts = []
    if blockers:
        parts.append(f"{blockers} blocker{'s' if blockers != 1 else ''}")
    if warnings:
        parts.append(f"{warnings} warning{'s' if warnings != 1 else ''}")
    lines = [f"We found {' and '.join(parts)} in that file:", ""]
    for issue in issues[:5]:
        lines.append(f"- [{issue.get('severity', 'issue').upper()}] {issue.get('message', '')}")
    if len(issues) > 5:
        lines.append(f"...and {len(issues) - 5} more.")
    return "\n".join(lines)


def send_immediate_nudge(email: str, result: dict) -> None:
    """Fire-and-forget. Call right after tc_gate.save_email() succeeds,
    once you have the check `result` dict from tc_audit.check_tc_file."""
    subject = "Your TC File Check results"
    body = (
        f"Thanks for using TC File Check on txtanoffer.com.\n\n"
        f"{issue_summary(result)}\n\n"
        f"Want to skip this step entirely next time? TxtAnOffer drafts your "
        f"TREC 20-19 by text message -- property address, sales price, and "
        f"closing date fill in correctly every time, so there's nothing "
        f"left to catch.\n\n"
        f"Try it free -- 3 offers, no card required: {PRICING_URL}\n\n"
        f"-- TxtAnOffer"
    )

    def _do():
        try:
            send_plain_email(email, subject, body)
        except Exception as e:
            print(f"[tc_nudge] immediate send failed for {email}: {e}")

    threading.Thread(target=_do, daemon=True).start()


def _check_due() -> bool:
    try:
        with open(_STATE_FILE) as f:
            last_run = float(f.read().strip())
    except (FileNotFoundError, ValueError):
        return True
    return (time.time() - last_run) > _CHECK_INTERVAL_SECONDS


def _mark_checked():
    try:
        with open(_STATE_FILE, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def run_followup_if_due() -> None:
    """Day-3 re-engagement email for anyone who gave an email but hasn't
    been nudged again. Call from the same high-traffic route reminders.py
    and cleanup.py are piggybacked on."""
    if not _check_due():
        return
    _mark_checked()

    def _do():
        try:
            for client in clients_needing_followup(FOLLOWUP_DAYS):
                subject = "Still checking TREC files by hand?"
                body = (
                    f"A few days ago you ran TC File Check on txtanoffer.com to catch "
                    f"missing fields and initials before title could kick a file back.\n\n"
                    f"If you're still filling out TREC 20-19s by hand, TxtAnOffer drafts "
                    f"them by text message instead -- address, price, and closing date "
                    f"auto-fill correctly, so there's nothing left to catch.\n\n"
                    f"Try it free -- 3 offers, no card required: {PRICING_URL}\n\n"
                    f"-- TxtAnOffer"
                )
                result = send_plain_email(client["email"], subject, body)
                if result.get("success"):
                    mark_followup_sent(client["client_id"])
        except Exception as e:
            print(f"[tc_nudge] followup scan failed: {e}")

    threading.Thread(target=_do, daemon=True).start()
