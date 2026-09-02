"""
reminders.py -- proactively texts agents before three real deadlines on
their offer: the closing date, the option period's termination deadline
(once accepted), and the Paragraph 5 earnest money / option fee delivery
deadline (also once accepted). Financing-approval deadlines still aren't
captured anywhere (never collected from the agent), so we don't guess at
a date nobody actually agreed to -- but the option period *is* collected
(parser.py's inspection_days, e.g. "10 day option" in the original text)
and stored per-offer.

Piggybacks on request traffic the same way cleanup.py does -- checked at
most once per _CHECK_INTERVAL_SECONDS -- rather than requiring a separate
scheduler/cron to be set up on Railway.

The option and earnest-money reminders are anchored on thread_responded_at
(the Effective Date proxy -- see deadlines.py), NOT created_at, because both
periods are contractually "N days after the Effective Date," which can be
days or weeks after the offer was drafted. An earlier version of the option
reminder anchored on created_at instead, which understated the real
deadline for any offer that took more than a few hours to get accepted --
fixed alongside adding the earnest-money reminder since they share the same
anchor. The closing reminder stays anchored on created_at: that date is
fixed at drafting time (created_at + close_days) and printed as such on the
contract itself (see pdf_filler.py), so it never depends on acceptance.
"""
import os
import sqlite3
import time
import threading
from datetime import datetime, timedelta

from deadlines import earnest_money_deadline, option_end_date

DB_PATH = os.environ.get("DATABASE_PATH", "subscriptions.db")
CLOSING_REMINDER_DAYS_BEFORE = int(os.environ.get("CLOSING_REMINDER_DAYS_BEFORE", 3))
# Option periods are usually much shorter than the time to closing (often
# just 7-10 days total), so a shorter lead time than the closing reminder
# keeps this from firing the same day the offer was sent while still
# leaving real time to act before the unrestricted right to terminate ends.
OPTION_REMINDER_DAYS_BEFORE = int(os.environ.get("OPTION_REMINDER_DAYS_BEFORE", 2))
# Earnest money/option fee is due just 3 days after the Effective Date
# (Paragraph 5) -- a 1-day lead time is the most this window allows while
# still giving the agent a real heads-up before it's due.
EARNEST_MONEY_REMINDER_DAYS_BEFORE = int(os.environ.get("EARNEST_MONEY_REMINDER_DAYS_BEFORE", 1))

_STATE_FILE = os.environ.get("REMINDER_STATE_FILE", ".last_reminder_check")
_CHECK_INTERVAL_SECONDS = 6 * 3600


def init_reminders_table():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reminders_sent (
            offer_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY (offer_id, kind)
        )
    """)
    conn.commit()
    conn.close()


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


def _due_offers():
    """Real (non-demo) offers whose closing date is exactly
    CLOSING_REMINDER_DAYS_BEFORE days away and haven't already gotten one."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, phone, address, close_days, created_at
        FROM offers
        WHERE phone NOT LIKE '%demo%'
          AND id NOT IN (SELECT offer_id FROM reminders_sent WHERE kind = 'closing')
    """)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()

    target_date = (datetime.utcnow() + timedelta(days=CLOSING_REMINDER_DAYS_BEFORE)).date()
    due = []
    for r in rows:
        try:
            created = datetime.fromisoformat(r["created_at"])
        except (ValueError, TypeError):
            continue
        closing_date = (created + timedelta(days=r["close_days"])).date()
        if closing_date == target_date:
            r["closing_date"] = closing_date
            due.append(r)
    return due


def _due_offers_option():
    """Accepted offers with a specified option period whose termination
    deadline (counted from the Effective Date, not from when the offer was
    drafted -- see module docstring) is exactly OPTION_REMINDER_DAYS_BEFORE
    days away and haven't already gotten one."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, phone, address, option_days, thread_responded_at
        FROM offers
        WHERE phone NOT LIKE '%demo%'
          AND option_days IS NOT NULL
          AND thread_status = 'accept'
          AND id NOT IN (SELECT offer_id FROM reminders_sent WHERE kind = 'option')
    """)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()

    target_date = (datetime.utcnow() + timedelta(days=OPTION_REMINDER_DAYS_BEFORE)).date()
    due = []
    for r in rows:
        try:
            effective = datetime.fromisoformat(r["thread_responded_at"]).date()
        except (ValueError, TypeError):
            continue
        end_date = option_end_date(effective, r["option_days"])
        if end_date == target_date:
            r["option_end_date"] = end_date
            due.append(r)
    return due


def _due_offers_earnest_money():
    """Accepted offers whose Paragraph 5 earnest money / option fee
    delivery deadline (3 days after the Effective Date, TREC day-counting
    rule applied -- see deadlines.py) is exactly
    EARNEST_MONEY_REMINDER_DAYS_BEFORE days away and haven't already gotten
    one."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, phone, address, thread_responded_at
        FROM offers
        WHERE phone NOT LIKE '%demo%'
          AND thread_status = 'accept'
          AND id NOT IN (SELECT offer_id FROM reminders_sent WHERE kind = 'earnest_money')
    """)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()

    target_date = (datetime.utcnow() + timedelta(days=EARNEST_MONEY_REMINDER_DAYS_BEFORE)).date()
    due = []
    for r in rows:
        try:
            effective = datetime.fromisoformat(r["thread_responded_at"]).date()
        except (ValueError, TypeError):
            continue
        deadline = earnest_money_deadline(effective)
        if deadline == target_date:
            r["earnest_money_deadline"] = deadline
            due.append(r)
    return due


def _mark_sent(offer_id, kind="closing"):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR IGNORE INTO reminders_sent (offer_id, kind, sent_at)
        VALUES (?, ?, ?)
    """, (offer_id, kind, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


def run_reminders_if_due(send_sms_fn):
    """send_sms_fn: callable(to, body) -> bool, e.g. app.py's twilio_send_sms.
    Passed in rather than imported to avoid a reminders<->app import cycle."""
    if not _check_due():
        return
    _mark_checked()

    def _do():
        try:
            for offer in _due_offers():
                body = (
                    f"Reminder: {offer['address']} is scheduled to close on "
                    f"{offer['closing_date'].strftime('%B %d, %Y')} "
                    f"({CLOSING_REMINDER_DAYS_BEFORE} days from now). "
                    f"Text DASHBOARD to review."
                )
                if send_sms_fn(offer["phone"], body):
                    _mark_sent(offer["id"], "closing")

            for offer in _due_offers_option():
                body = (
                    f"Reminder: the option period on {offer['address']} ends "
                    f"{offer['option_end_date'].strftime('%B %d, %Y')} "
                    f"({OPTION_REMINDER_DAYS_BEFORE} days from now). "
                    f"Terminate before then if you're not moving forward. "
                    f"Text DASHBOARD to review."
                )
                if send_sms_fn(offer["phone"], body):
                    _mark_sent(offer["id"], "option")

            for offer in _due_offers_earnest_money():
                body = (
                    f"Reminder: earnest money + option fee for {offer['address']} "
                    f"is due {offer['earnest_money_deadline'].strftime('%B %d, %Y')} "
                    f"({EARNEST_MONEY_REMINDER_DAYS_BEFORE} day"
                    f"{'s' if EARNEST_MONEY_REMINDER_DAYS_BEFORE != 1 else ''} from now), per Paragraph 5. "
                    f"Text DASHBOARD to review."
                )
                if send_sms_fn(offer["phone"], body):
                    _mark_sent(offer["id"], "earnest_money")
        except Exception as e:
            print(f"[reminders] Error: {e}")

    threading.Thread(target=_do, daemon=True).start()


init_reminders_table()
