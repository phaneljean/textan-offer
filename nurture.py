"""
nurture.py -- three follow-up texts for free-trial SMS users, added
2026-10-23 (Phanel approved). In the 30 days before this, 3 phones texted an
offer and 0 paid, and nothing followed up after the draft link went out.

  offer1   ~24h after their 1st offer, if they haven't sent a 2nd:
           where the drafts live (DASHBOARD) + saving earnest/option
           defaults once (PROFILE). Both are real SMS commands in app.py.
  offer2   ~24h after their 2nd offer, if they haven't sent a 3rd:
           "1 free offer left" + a usage tip the parser really handles.
  paywall  3 days after they hit limit_reached, if still unpaid.
           Promotional, so at most ONE, ever.

Off unless SMS_NURTURE_ENABLED is set, so deploying this doesn't turn it on.
The paywall step also needs SMS_NURTURE_PAYWALL_ENABLED (see paywall_enabled).
Phanel confirms the compliance items (TX SB 140 / Bus. & Com. Code ch. 301,
TCPA, toll-free verification) first.

Scheduling piggybacks on request traffic the same way reminders.py and
cleanup.py do -- no Railway cron to set up -- but checks every 15 minutes
instead of every 6 hours, because the quiet-hours window and the ~24h
timing need finer resolution. Each step is claimed in sms_nurture
(PRIMARY KEY phone+step, on the DATABASE_PATH volume) BEFORE the send, so
gunicorn's 3 workers scanning at once, or a redeploy mid-scan, can never
text the same step twice. A failed send stays claimed and is not retried:
a missed tip beats a double text.

A step only fires inside a 48h window after it comes due. Without that,
turning the flag on would text every trial user who ever existed about an
offer from months ago.
"""
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from subscriptions import FREE_OFFER_LIMIT, get_user, is_admin_phone

DB_PATH = os.environ.get("DATABASE_PATH", "subscriptions.db")

OFFER_STEP_DELAY = timedelta(hours=24)
PAYWALL_STEP_DELAY = timedelta(days=3)
# How long after coming due a step may still go out. Longer than the
# longest quiet-hours hold (~15h overnight), short enough that nothing
# stale goes out when the flag is first switched on.
STALE_AFTER = timedelta(hours=48)

_CHECK_INTERVAL_SECONDS = 15 * 60
_last_check = 0.0  # per worker; the claim row is what actually prevents duplicates

PRICING_LINK = "txtanoffer.com/pricing?src=sms_nurture_paywall"
STOP_LINE = "Reply STOP to opt out."

# Twilio's default opt-out / opt-in keywords. CANCEL is on Twilio's opt-out
# list too, but app.py also uses it to cancel a pending draft -- for nurture
# purposes it still counts as an opt-out (skipping a tip is harmless; texting
# someone who meant "stop" isn't).
OPT_OUT_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "OPTOUT", "REVOKE"}
OPT_IN_WORDS = {"START", "UNSTOP"}

# US NANP mobile/landline shape only. Demo/landing source ids ("landing-demo",
# demo keys) and anything foreign are not textable here.
_US_PHONE = re.compile(r"^\+1[2-9]\d{2}[2-9]\d{6}$")

# Quiet hours, US Central: 9am-8pm, but Sunday starts at noon -- Texas Bus. &
# Com. Code 301.051 only allows telephone solicitation 9am-9pm Mon-Sat and
# noon-9pm Sunday, and SB 140 (eff. 2025-09-01) brought texts under it.
# Mountain-time area codes (El Paso + Colorado) start an hour later so the
# 9am floor holds in the recipient's own time zone too.
WINDOW_START_HOUR = 9
WINDOW_END_HOUR = 20
SUNDAY_START_HOUR = 12
MOUNTAIN_AREA_CODES = {"915", "303", "720", "719", "970", "983"}


def enabled() -> bool:
    return os.environ.get("SMS_NURTURE_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def paywall_enabled() -> bool:
    """The paywall reminder is promotional. /privacy currently says "We do
    not send marketing or promotional messages" and the toll-free
    verification describes transactional use, so it has its own flag on top
    of SMS_NURTURE_ENABLED: the two account tips can go live without it."""
    return enabled() and os.environ.get("SMS_NURTURE_PAYWALL_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def init_nurture_tables():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sms_nurture (
            phone TEXT NOT NULL,
            step TEXT NOT NULL,
            status TEXT NOT NULL,
            trigger_at TEXT,
            claimed_at TEXT NOT NULL,
            sent_at TEXT,
            PRIMARY KEY (phone, step)
        )
    """)
    # Opt-outs are kept here, not only in `events`, because cleanup.py
    # deletes event rows after 90 days and a STOP has to outlive that.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sms_opt_outs (
            phone TEXT PRIMARY KEY,
            opted_out_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def record_opt_keyword(phone: str, body: str, opt_out_type: str = "") -> str:
    """Called on every inbound SMS. Twilio sends OptOutType=STOP/START when
    Advanced Opt-Out is on; the keyword match covers it being off. Returns
    "STOP", "START" or "" so the caller can skip its normal reply."""
    word = (body or "").strip().upper()
    kind = (opt_out_type or "").strip().upper()
    if kind not in ("STOP", "START"):
        kind = "STOP" if word in OPT_OUT_WORDS else "START" if word in OPT_IN_WORDS else ""
    if not kind or not phone:
        return kind
    conn = sqlite3.connect(DB_PATH)
    if kind == "STOP":
        conn.execute("INSERT OR REPLACE INTO sms_opt_outs (phone, opted_out_at) VALUES (?, ?)",
                     (phone, datetime.utcnow().isoformat()))
    else:
        conn.execute("DELETE FROM sms_opt_outs WHERE phone = ?", (phone,))
    conn.commit()
    conn.close()
    return kind


def is_opted_out(phone: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        if conn.execute("SELECT 1 FROM sms_opt_outs WHERE phone = ?", (phone,)).fetchone():
            return True
        # STOPs texted before this table existed only live in `events`
        # (sms_received, body as typed). Latest opt keyword wins.
        rows = conn.execute("""
            SELECT metadata FROM events
            WHERE event_type = 'sms_received' AND phone = ?
            ORDER BY created_at DESC
        """, (phone,)).fetchall()
        # Twilio refused an earlier send: 21610 = recipient unsubscribed,
        # 21211/21614 = invalid or non-mobile number.
        failed = conn.execute("""
            SELECT 1 FROM events WHERE event_type = 'sms_send_failed' AND phone = ?
              AND (metadata LIKE '%21610%' OR metadata LIKE '%21211%' OR metadata LIKE '%21614%')
        """, (phone,)).fetchone()
    finally:
        conn.close()
    if failed:
        return True
    import json
    for (meta,) in rows:
        try:
            word = (json.loads(meta or "{}").get("body") or "").strip().upper()
        except ValueError:
            continue
        if word in OPT_OUT_WORDS:
            return True
        if word in OPT_IN_WORDS:
            return False
    return False


def _central_now(now_utc: datetime):
    from zoneinfo import ZoneInfo
    return now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/Chicago"))


def in_send_window(phone: str, now_utc: datetime) -> bool:
    """True if a nurture text may go out to `phone` right now. Fails closed
    (no send) if the time zone data can't be loaded."""
    try:
        local = _central_now(now_utc)
    except Exception as e:
        print(f"[nurture] no tz data, holding sends: {e}")
        return False
    start = SUNDAY_START_HOUR if local.weekday() == 6 else WINDOW_START_HOUR
    if phone[2:5] in MOUNTAIN_AREA_CODES:
        start += 1
    return start <= local.hour < WINDOW_END_HOUR


def _skip_reason(phone: str):
    """Why this phone gets no nurture text, or None. Re-checked right before
    every send so a subscribe/STOP since the scan started still wins."""
    if not _US_PHONE.match(phone or ""):
        return "invalid_number"
    if is_admin_phone(phone):
        return "internal"
    user = get_user(phone)
    if not user:
        return "no_user"
    if user["is_subscribed"]:
        return "subscribed"
    if user.get("brokerage_id"):
        return "brokerage_roster"
    if is_opted_out(phone):
        return "opted_out"
    return None


def _parse(ts):
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def due_steps(now_utc: datetime):
    """[(phone, step, trigger_at, is_co)] that are due, not stale and not
    yet claimed. Skips and quiet hours are applied by the caller."""
    conn = sqlite3.connect(DB_PATH)
    try:
        claimed = {(p, s) for p, s in conn.execute("SELECT phone, step FROM sms_nurture")}
        due = []

        # offer1/offer2: the user's latest offer, while offer_count still
        # says it was their 1st/2nd (so a newer offer cancels the step).
        rows = conn.execute("""
            SELECT u.phone, u.offer_count, MAX(o.created_at),
                   (SELECT filename FROM offers WHERE phone = u.phone ORDER BY created_at DESC LIMIT 1)
            FROM users u JOIN offers o ON o.phone = u.phone
            WHERE u.offer_count IN (1, 2) AND COALESCE(u.is_subscribed, 0) = 0
              AND u.brokerage_id IS NULL
            GROUP BY u.phone
        """).fetchall()
        for phone, count, last_at, filename in rows:
            step = f"offer{count}"
            at = _parse(last_at)
            if not at or (phone, step) in claimed:
                continue
            if at + OFFER_STEP_DELAY <= now_utc < at + OFFER_STEP_DELAY + STALE_AFTER:
                due.append((phone, step, at, (filename or "").startswith("CBS1_")))

        # paywall: any limit_reached in the window counts; one per phone ever.
        if not paywall_enabled():
            return due
        lo = (now_utc - PAYWALL_STEP_DELAY - STALE_AFTER).isoformat()
        hi = (now_utc - PAYWALL_STEP_DELAY).isoformat()
        rows = conn.execute("""
            SELECT phone, MIN(created_at) FROM events
            WHERE event_type = 'limit_reached' AND phone IS NOT NULL
              AND created_at > ? AND created_at <= ?
            GROUP BY phone
        """, (lo, hi)).fetchall()
        for phone, at in rows:
            if (phone, "paywall") not in claimed:
                due.append((phone, "paywall", _parse(at), False))
    finally:
        conn.close()
    return due


def message_for(step: str, is_co: bool = False) -> str:
    # Plain GSM-7 only (see app.py format_offer_confirmation): no em dashes
    # or curly quotes, which would flip the text to UCS-2 and triple the cost.
    if step == "offer1":
        defaults = "your earnest %" if is_co else "your earnest % and option fee"
        return ("TxtAnOffer: your draft is saved. Text DASHBOARD anytime to see your drafts. "
                f"Text PROFILE to save {defaults} once, and every new draft fills in your way. "
                + STOP_LINE)
    if step == "offer2":
        left = FREE_OFFER_LIMIT - 2
        example = ("725k 3% 21day FHA 10 day inspection 123 Main St, Denver, CO" if is_co
                   else "725k 3% 21day FHA 10 day option 123 Main St, Austin, TX")
        return (f"TxtAnOffer: {left} free offer{'s' if left != 1 else ''} left. Tip: put the loan type and "
                f"{'inspection' if is_co else 'option'} days in the same text, ex: {example}. "
                "Text HELP for all commands. " + STOP_LINE)
    if step == "paywall":
        return ("TxtAnOffer: got a buyer this week? Unlimited offers by text, $40/mo, cancel anytime: "
                f"{PRICING_LINK} " + STOP_LINE)
    raise ValueError(step)


def _claim(phone, step, trigger_at, now_utc) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute("""
            INSERT OR IGNORE INTO sms_nurture (phone, step, status, trigger_at, claimed_at)
            VALUES (?, ?, 'sending', ?, ?)
        """, (phone, step, trigger_at.isoformat() if trigger_at else None, now_utc.isoformat()))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _finish(phone, step, status, now_utc):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE sms_nurture SET status = ?, sent_at = ? WHERE phone = ? AND step = ?",
                 (status, now_utc.isoformat() if status == "sent" else None, phone, step))
    conn.commit()
    conn.close()


def run_nurture(send_sms_fn, now_utc: datetime = None) -> list:
    """One scan, inline. Returns [(phone, step)] actually sent (for tests).
    send_sms_fn: callable(to, body) -> bool, e.g. app.py's twilio_send_sms."""
    if not enabled():
        return []
    from analytics import track_event
    now_utc = now_utc or datetime.utcnow()
    sent = []
    for phone, step, trigger_at, is_co in due_steps(now_utc):
        if _skip_reason(phone):
            continue  # not claimed: a skip is re-evaluated, never "used up"
        if not in_send_window(phone, now_utc):
            continue  # held until the window opens; still inside STALE_AFTER
        if not _claim(phone, step, trigger_at, now_utc):
            continue  # another worker got it
        ok = bool(send_sms_fn(phone, message_for(step, is_co)))
        _finish(phone, step, "sent" if ok else "failed", now_utc)
        if ok:
            track_event("nurture_sent", phone, {"step": step})
            sent.append((phone, step))
    return sent


def run_nurture_if_due(send_sms_fn):
    """Called on request traffic; scans at most every _CHECK_INTERVAL_SECONDS
    per worker, in a background thread so it never delays the response."""
    global _last_check
    if not enabled() or time.time() - _last_check < _CHECK_INTERVAL_SECONDS:
        return
    _last_check = time.time()

    def _do():
        try:
            run_nurture(send_sms_fn)
        except Exception as e:
            print(f"[nurture] Error: {e}")

    threading.Thread(target=_do, daemon=True).start()


init_nurture_tables()
