"""
analytics.py — Track key conversion metrics
"""
import sqlite3
import os
import hmac
from datetime import datetime, timedelta
from flask import request, has_request_context

DB_PATH = os.environ.get("DATABASE_PATH", "subscriptions.db")

# Lets the site owner exclude their own testing from every metric on
# /analytics -- deliberately NOT an IP allowlist: home wifi, phone LTE, a
# coffee shop, a VPN all give a different IP, so a fixed IP list needs
# constant upkeep and silently stops working the moment it's stale. A
# cookie survives all of that; it just needs setting once per browser/
# device via app.py's /internal-mode route. Reuses ANALYTICS_PASSWORD --
# the same secret already needed to view /analytics at all -- rather than
# adding a second one to manage.
_INTERNAL_COOKIE = "ta_internal"


def _is_internal_traffic() -> bool:
    if not has_request_context():
        return False
    secret = os.environ.get("ANALYTICS_PASSWORD", "")
    if not secret:
        return False
    return hmac.compare_digest(request.cookies.get(_INTERNAL_COOKIE, ""), secret)


def init_analytics_tables():
    """Create analytics tables"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS internal_visitors (
            visitor TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            phone TEXT,
            metadata TEXT,
            created_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()

def set_internal_visitor(visitor: str, internal: bool):
    """Remembers (or forgets) a browser's ta_vid as the owner's own. The
    ta_internal cookie already stops NEW events from that browser; this
    also lets get_daily_funnel()/get_top_referrers() drop page views it
    logged BEFORE /internal-mode was opened there."""
    conn = sqlite3.connect(DB_PATH)
    if internal:
        conn.execute("INSERT OR IGNORE INTO internal_visitors (visitor, created_at) VALUES (?, ?)",
                     (visitor, datetime.utcnow().isoformat()))
    else:
        conn.execute("DELETE FROM internal_visitors WHERE visitor = ?", (visitor,))
    conn.commit()
    conn.close()


def _internal_visitor_ids() -> set:
    conn = sqlite3.connect(DB_PATH)
    try:
        return {r[0] for r in conn.execute("SELECT visitor FROM internal_visitors")}
    finally:
        conn.close()


def track_event(event_type: str, phone: str = None, metadata: dict = None):
    """Track an analytics event. Silently a no-op for the site owner's own
    browser once flagged via /internal-mode -- see _is_internal_traffic().
    Every metric on /analytics is built from this one table, so this single
    chokepoint is enough to keep owner testing out of all of them."""
    if _is_internal_traffic():
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()

    import json
    metadata_json = json.dumps(metadata) if metadata else None

    cursor.execute("""
        INSERT INTO events (event_type, phone, metadata, created_at)
        VALUES (?, ?, ?, ?)
    """, (event_type, phone, metadata_json, now))

    conn.commit()
    conn.close()

def get_conversion_metrics(days: int = 30) -> dict:
    """Get conversion funnel metrics for last N days"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

    # Total signups
    cursor.execute("""
        SELECT COUNT(DISTINCT phone)
        FROM events
        WHERE event_type = 'offer_generated'
        AND created_at > ?
    """, (cutoff,))
    signups = cursor.fetchone()[0]

    # Trial completions
    cursor.execute("""
        SELECT COUNT(DISTINCT phone)
        FROM events
        WHERE event_type = 'trial_completed'
        AND created_at > ?
    """, (cutoff,))
    trial_completions = cursor.fetchone()[0]

    # Conversions
    cursor.execute("""
        SELECT COUNT(DISTINCT phone)
        FROM events
        WHERE event_type = 'subscription_created'
        AND created_at > ?
    """, (cutoff,))
    conversions = cursor.fetchone()[0]

    # Total offers
    cursor.execute("""
        SELECT COUNT(*)
        FROM events
        WHERE event_type = 'offer_generated'
        AND created_at > ?
    """, (cutoff,))
    total_offers = cursor.fetchone()[0]

    # Hit paywall
    cursor.execute("""
        SELECT COUNT(DISTINCT phone)
        FROM events
        WHERE event_type = 'limit_reached'
        AND created_at > ?
    """, (cutoff,))
    hit_paywall = cursor.fetchone()[0]

    conn.close()

    # Calculate rates
    trial_activation_rate = (trial_completions / signups * 100) if signups > 0 else 0
    paywall_to_paid = (conversions / hit_paywall * 100) if hit_paywall > 0 else 0
    overall_conversion = (conversions / signups * 100) if signups > 0 else 0

    return {
        "period_days": days,
        "signups": signups,
        "trial_completions": trial_completions,
        "hit_paywall": hit_paywall,
        "conversions": conversions,
        "total_offers": total_offers,
        "trial_activation_rate": round(trial_activation_rate, 1),
        "paywall_to_paid_rate": round(paywall_to_paid, 1),
        "overall_conversion_rate": round(overall_conversion, 1),
        "avg_offers_per_user": round(total_offers / signups, 1) if signups > 0 else 0,
    }

def get_revenue_metrics() -> dict:
    """Calculate revenue metrics"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT COUNT(*)
        FROM users
        WHERE is_subscribed = 1
    """)
    active_subs = cursor.fetchone()[0]

    conn.close()

    mrr = active_subs * 49
    arr = mrr * 12

    return {
        "active_subscribers": active_subs,
        "mrr": mrr,
        "arr": arr,
    }

def get_recent_sms(limit: int = 50) -> list:
    """Get recent SMS activity"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT phone, metadata, created_at
        FROM events
        WHERE event_type = 'sms_received'
        ORDER BY created_at DESC
        LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    import json
    results = []
    for row in rows:
        metadata = json.loads(row[1]) if row[1] else {}
        results.append({
            "phone": row[0],
            "body": metadata.get("body", ""),
            "created_at": row[2]
        })

    return results

def get_recent_sms_failures(limit: int = 20) -> list:
    """Get recent outbound SMS send failures (e.g. Twilio A2P 10DLC blocks)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT phone, metadata, created_at
        FROM events
        WHERE event_type = 'sms_send_failed'
        ORDER BY created_at DESC
        LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    import json
    results = []
    for row in rows:
        metadata = json.loads(row[1]) if row[1] else {}
        results.append({
            "phone": row[0],
            "error": metadata.get("error", ""),
            "body": metadata.get("body", ""),
            "created_at": row[2]
        })

    return results

def get_last_blocked_state(phone: str, hours: int = 72):
    """Most recent state a phone's offer was blocked for (see
    other_state_block_message in app.py), so a WAITLIST reply can be
    attributed to a specific state instead of just a bare signup. Only
    looks back `hours` so a reply days later after trying a real Texas
    address isn't mis-attributed to a stale block."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    cursor.execute("""
        SELECT metadata FROM events
        WHERE event_type = 'blocked_other_state' AND phone = ? AND created_at > ?
        ORDER BY created_at DESC LIMIT 1
    """, (phone, cutoff))
    row = cursor.fetchone()
    conn.close()
    if not row or not row[0]:
        return None
    import json
    return json.loads(row[0]).get("state")

def get_landing_visits_by_source(days: int = 30) -> list:
    """Raw homepage-visit counts grouped by ?src= attribution, regardless of
    whether the visitor ever signed up. Signups-by-source alone can't tell
    "nobody opened the link" apart from "people opened it and left" -- both
    look like silence. This answers that: a nonzero count here with zero
    matching signups means the link IS being clicked, just not converting;
    a zero count means the messages aren't being opened/clicked at all."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    cursor.execute("""
        SELECT metadata FROM events
        WHERE event_type = 'landing_visit' AND created_at > ?
    """, (cutoff,))
    rows = cursor.fetchall()
    conn.close()

    import json
    counts = {}
    for row in rows:
        metadata = json.loads(row[0]) if row[0] else {}
        source = metadata.get("source") or "direct"
        counts[source] = counts.get(source, 0) + 1

    return sorted(
        [{"source": source, "count": count} for source, count in counts.items()],
        key=lambda r: -r["count"]
    )

def get_tc_check_attempts_by_source(days: int = 30) -> list:
    """Same idea as get_landing_visits_by_source, one funnel step later:
    counts every real submission to /v1/tc/check (success OR error --
    wrong file type, corrupted PDF, whatever) grouped by the same ta_src
    attribution. Added 2026-09-09 specifically to answer "LinkedIn drives
    visits but conversions stay flat -- are visitors even touching the
    upload widget, or trying and hitting an error?" -- neither question
    was answerable before this, since the pre-existing 'tc_check' event
    only fires after a file is successfully parsed. Compare this against
    get_landing_visits_by_source's count for the same source: a big gap
    means visitors aren't engaging the widget at all (a page/copy
    problem); attempts close to visits but recognized/complete much
    lower (see get_tc_check_summary) means they're trying and something
    downstream is failing."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    cursor.execute("""
        SELECT metadata FROM events
        WHERE event_type = 'tc_check_attempted' AND created_at > ?
    """, (cutoff,))
    rows = cursor.fetchall()
    conn.close()

    import json
    counts = {}
    for row in rows:
        metadata = json.loads(row[0]) if row[0] else {}
        source = metadata.get("source") or "direct"
        counts[source] = counts.get(source, 0) + 1

    return sorted(
        [{"source": source, "count": count} for source, count in counts.items()],
        key=lambda r: -r["count"]
    )


def get_tc_check_attempts_by_page(days: int = 30) -> list:
    """Same 'tc_check_attempted' events as get_tc_check_attempts_by_source,
    grouped by source_page instead of campaign source -- homepage widget vs
    the dedicated /tc-check page. Added 2026-09-11 to answer a different
    question than the source breakdown: not which channel drove the visit,
    but which UI they actually used once there. 'unknown' covers events
    tracked before this field existed, or a request that didn't set it."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    cursor.execute("""
        SELECT metadata FROM events
        WHERE event_type = 'tc_check_attempted' AND created_at > ?
    """, (cutoff,))
    rows = cursor.fetchall()
    conn.close()

    import json
    counts = {}
    for row in rows:
        metadata = json.loads(row[0]) if row[0] else {}
        page = metadata.get("source_page") or "unknown"
        counts[page] = counts.get(page, 0) + 1

    return sorted(
        [{"page": page, "count": count} for page, count in counts.items()],
        key=lambda r: -r["count"]
    )


def get_daily_funnel(days: int = 14) -> list:
    """One row per UTC day, newest first: visitors -> widget attempts ->
    recognized files, plus email intake. Added 2026-10-02: every other
    metric on /analytics is a 30-day or 24h total, so "when did traffic
    stop?" had no answer. Visitors come from 'page_view' (every non-bot
    load of / and /tc-check, tagged or not -- unlike 'landing_visit',
    which only fires on ?src= links); days before page_view existed show
    0 visitors, not missing data. Every day in the window gets a row, so a
    dead day reads as an explicit row of zeros instead of a gap."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    cursor.execute("""
        SELECT event_type, metadata, created_at FROM events
        WHERE created_at > ? AND event_type IN
            ('page_view', 'page_engagement', 'tc_check_attempted', 'tc_check_demo_used', 'tc_check', 'tc_check_email_captured', 'tc_check_email_rejected')
    """, (cutoff,))
    rows = cursor.fetchall()
    conn.close()

    import json
    today = datetime.utcnow().date()
    by_day = {}
    for i in range(days):
        d = (today - timedelta(days=i)).isoformat()
        by_day[d] = {"date": d, "visitors": set(), "mobile": set(), "js_ok": set(), "hero_cta": set(), "hero_sample": set(),
                     "dropzone_seen": set(), "stay_10s": set(), "views": 0, "attempts": 0, "demos": 0,
                     "recognized": 0, "emails_captured": 0, "email_checks": 0, "email_junk": 0}
    internal = _internal_visitor_ids()
    for event_type, metadata_json, created_at in rows:
        day = by_day.get(created_at[:10])
        if day is None:
            continue
        metadata = json.loads(metadata_json) if metadata_json else {}
        if event_type in ("page_view", "page_engagement") and metadata.get("visitor") in internal:
            continue
        if event_type == "page_view":
            day["views"] += 1
            day["visitors"].add(metadata.get("visitor") or "")
            if metadata.get("device") == "mobile":
                day["mobile"].add(metadata.get("visitor") or "")
        elif event_type == "page_engagement":
            key = metadata.get("type")
            if key in ("js_ok", "hero_cta", "hero_sample", "dropzone_seen", "stay_10s"):
                day[key].add(metadata.get("visitor") or "")
        elif event_type == "tc_check_attempted":
            day["attempts"] += 1
        elif event_type == "tc_check_demo_used":
            day["demos"] += 1
        elif event_type == "tc_check_email_rejected":
            day["email_junk"] += 1
        elif event_type == "tc_check_email_captured":
            day["emails_captured"] += 1
        elif event_type == "tc_check":
            if metadata.get("source") == "email":
                day["email_checks"] += 1
            elif metadata.get("recognized"):
                day["recognized"] += 1

    result = []
    for d in sorted(by_day, reverse=True):
        day = by_day[d]
        for key in ("visitors", "mobile", "js_ok", "hero_cta", "hero_sample", "dropzone_seen", "stay_10s"):
            day[key] = len(day[key])
        result.append(day)
    return result


def get_recent_visitors(hours: int = 48, limit: int = 150) -> list:
    """One row per visitor (ta_vid) seen in the last `hours`, newest first:
    pages loaded, referrer, ?src=, device, user agent (logged from
    2026-10-07), and which engagement beacons fired. Added 2026-10-07 so
    "who were today's visitors?" can be answered row by row instead of
    guessed from daily totals. Bots and link scanners don't keep cookies,
    so each of their hits shows up as a separate one-page visitor with no
    "real browser" -- that pattern is the tell."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    cursor.execute("""
        SELECT event_type, metadata, created_at FROM events
        WHERE created_at > ? AND event_type IN ('page_view', 'page_engagement')
        ORDER BY created_at
    """, (cutoff,))
    rows = cursor.fetchall()
    conn.close()

    import json
    internal = _internal_visitor_ids()
    visitors = {}
    for event_type, metadata_json, created_at in rows:
        metadata = json.loads(metadata_json) if metadata_json else {}
        vid = metadata.get("visitor") or ""
        if not vid or vid in internal:
            continue
        v = visitors.setdefault(vid, {"visitor": vid, "first": created_at, "last": created_at, "pages": {},
                                      "referrers": set(), "sources": set(), "devices": set(), "ua": "", "events": set()})
        v["last"] = created_at
        if event_type == "page_view":
            page = metadata.get("page") or "?"
            v["pages"][page] = v["pages"].get(page, 0) + 1
            v["referrers"].add(metadata.get("referrer") or "(none)")
            v["sources"].add(metadata.get("source") or "direct")
            v["devices"].add(metadata.get("device") or "?")
            if metadata.get("ua") and not v["ua"]:
                v["ua"] = metadata["ua"]
        else:
            v["events"].add(metadata.get("type") or "")
    result = sorted(visitors.values(), key=lambda v: v["first"], reverse=True)[:limit]
    for v in result:
        for key in ("referrers", "sources", "devices"):
            v[key] = sorted(v[key])
    return result


def get_archive_early_access(limit: int = 200) -> list:
    """Sign-ups from /archive (contract archive early access), newest first."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT metadata, created_at FROM events WHERE event_type = 'archive_early_access'
        ORDER BY created_at DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    import json
    out = []
    for metadata_json, created_at in rows:
        m = json.loads(metadata_json) if metadata_json else {}
        out.append({"email": m.get("email", ""), "brokerage": m.get("brokerage", ""), "agents": m.get("agents", ""),
                    "source": m.get("source", ""), "created_at": created_at})
    return out


def get_top_referrers(days: int = 30, limit: int = 15) -> list:
    """Referring site for 'page_view' events, by host ("" -> "(none)": typed
    URL, bookmark, or an app that strips the Referer such as LinkedIn's
    mobile app). Complements the ?src= tables, which only see tagged links."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    cursor.execute("""
        SELECT metadata FROM events
        WHERE event_type = 'page_view' AND created_at > ?
    """, (cutoff,))
    rows = cursor.fetchall()
    conn.close()

    import json
    internal = _internal_visitor_ids()
    counts = {}
    for row in rows:
        metadata = json.loads(row[0]) if row[0] else {}
        if metadata.get("visitor") in internal:
            continue
        ref = metadata.get("referrer") or "(none)"
        counts[ref] = counts.get(ref, 0) + 1

    return sorted(
        [{"referrer": ref, "count": count} for ref, count in counts.items()],
        key=lambda r: -r["count"]
    )[:limit]

TC_ISSUE_LABELS = {
    "unrecognized": "Not a recognized TREC 20-19 template",
    "address": "Property address blank",
    "city": "City blank",
    "county": "County blank",
    "buyer_name": "Buyer legal name blank",
    "seller_name": "Seller legal name blank",
    "escrow_agent_name": "Escrow Agent name blank",
    "earnest_money_amount": "Earnest money amount blank",
    "option_fee_amount": "Option fee amount blank",
    "title_company": "Title Company blank",
    "effective_date": "Effective Date blank",
    "initials_buyer": "Buyer initials missing (some page)",
    "initials_seller": "Seller initials missing (some page)",
    "loan_amount_mismatch": "40-11 loan amount doesn't match contract",
    "addendum_checkbox_mismatch": "Third Party Financing checkbox disagrees with addendum",
    "initials_mismatch": "Initials don't match the named party",
    "sales_price_math": "3A + 3B doesn't equal Sales Price",
    "closing_date_invalid": "Closing date is not a real date",
    "option_days_blank": "Option fee set but option days blank",
    "check_one_conflict": "Two boxes checked in a check-one section",
    "disclosure_days_blank": "7B(2) disclosure days blank",
    "broker_contribution_incomplete": "12B broker contribution incomplete",
    "poa_addendum_missing": "POA disclosed, addendum not checked",
    "receipt_mismatch": "Page 12 receipt disagrees with 5A",
    "header_address": "Page header address blank or wrong",
    "email_invalid": "Malformed notice email",
}

def get_tc_check_summary(days: int = 30, sender_emails: set = None) -> dict:
    """Usage + funnel summary for the TC file-check tool (/v1/tc/check).
    Separate from the rest of this module's metrics -- that endpoint
    tracks 'tc_check' / 'tc_check_email_captured' events (see app.py's
    tc_check()) that nothing else here surfaces, so this was invisible on
    the dashboard until now.

    email_capture_rate is emails_captured / web_count -- the site-wide web
    channel, not a "gated uploads" subset. There's no more gate (removed
    2026-09-06): every web upload shows the full report regardless of
    email, so the only honest capture rate now is against every web check,
    not some filtered slice of them.

    issue_frequency answers "what checks fire most" directly from
    production traffic -- each issue in tc_audit.py's CHECKED_FIELDS (plus
    the derived checks: Effective Date, initials, addendum consistency)
    carries a stable 'key' precisely so it can be tallied here instead of
    parsed back out of free-text messages. Percentages are of *recognized*
    uploads, since an unrecognized file can't fire any real check.

    sender_emails: when given (a lowercase-normalized set), restricts the
    whole summary to tc_check events whose 'sender' matches one of those
    addresses -- this is what lets /broker/dashboard show a roster-specific
    read instead of the sitewide one. Both channels can carry a sender now:
    the email-forward path always has one (it's the From address), and the
    web path only does once that browser's gate email has been captured
    (see app.py's tc_check()) -- an anonymous web check with no email on
    file for it yet can never match a roster and is correctly excluded.
    Passing this drops the gate/email-capture funnel numbers entirely,
    since those happen on the anonymous web tool and have no roster
    meaning."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    cursor.execute("""
        SELECT metadata FROM events
        WHERE event_type = 'tc_check' AND created_at > ?
    """, (cutoff,))
    rows = cursor.fetchall()

    if sender_emails is not None:
        emails_captured = 0
    else:
        cursor.execute("""
            SELECT COUNT(*) FROM events
            WHERE event_type = 'tc_check_email_captured' AND created_at > ?
        """, (cutoff,))
        emails_captured = cursor.fetchone()[0]
    conn.close()

    import json
    recognized = complete = 0
    web_count = email_count = email_known_sender = 0
    issue_counts = {}
    total = 0
    for row in rows:
        metadata = json.loads(row[0]) if row[0] else {}
        if sender_emails is not None:
            if (metadata.get("sender") or "").strip().lower() not in sender_emails:
                continue
        total += 1
        if metadata.get("recognized"):
            recognized += 1
        if metadata.get("complete"):
            complete += 1
        for key in metadata.get("issue_keys") or []:
            issue_counts[key] = issue_counts.get(key, 0) + 1

        # Channel split -- app.py's tc_check() (web upload) never sets
        # 'source' in its tracked metadata, only tc_check_email_inbound()
        # does (source: "email"), so an absent key means web. known_sender
        # is only meaningful on the email path (see tc_check_email_inbound's
        # find_by_email() lookup) -- it's the number that actually answers
        # whether this channel reaches people the web tool never would.
        if metadata.get("source") == "email":
            email_count += 1
            if metadata.get("known_sender"):
                email_known_sender += 1
        else:
            web_count += 1

    issue_frequency = sorted(
        [
            {
                "key": key,
                "label": TC_ISSUE_LABELS.get(key, key),
                "count": count,
                "pct_of_recognized": round(count / recognized * 100, 1) if recognized else 0,
            }
            for key, count in issue_counts.items()
        ],
        key=lambda r: -r["count"],
    )

    return {
        "total": total,
        "recognized": recognized,
        "complete": complete,
        "completion_rate": round(complete / recognized * 100, 1) if recognized else 0,
        "emails_captured": emails_captured,
        "email_capture_rate": round(emails_captured / web_count * 100, 1) if web_count else 0,
        "issue_frequency": issue_frequency,
        "web_count": web_count,
        "email_count": email_count,
        "email_known_sender": email_known_sender,
        "email_new_sender_pct": round((email_count - email_known_sender) / email_count * 100, 1) if email_count else 0,
    }

def get_tc_check_count_for_sender(email: str) -> int:
    """Lifetime count of tc_check events carrying this sender, across both
    channels (email-forward always has one; web only once that browser's
    gate email has been captured -- see app.py's tc_check()). Used to put
    a genuine "this is your Nth file" line in the email-forward reply --
    the cheapest version of the habit-forming usage messaging idea in
    [[project_txtanoffer_gtm_roadmap]], since the sender is already
    tracked on every event. No time cutoff, unlike get_tc_check_summary --
    this is meant to read as a running lifetime tally, not a 30-day stat."""
    email = (email or "").strip().lower()
    if not email:
        return 0
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT metadata FROM events WHERE event_type = 'tc_check'")
    rows = cursor.fetchall()
    conn.close()

    import json
    count = 0
    for (metadata_json,) in rows:
        metadata = json.loads(metadata_json) if metadata_json else {}
        if "reason" in metadata:
            continue  # no_pdf / unreadable -- not an actual file checked
        if (metadata.get("sender") or "").strip().lower() == email:
            count += 1
    return count


def get_tc_check_repeat_senders(within_days: int = 14) -> dict:
    """How many distinct people have come back for a 2nd TC Check within
    `within_days` of their first one, lifetime (not a rolling 30-day
    window like get_tc_check_summary) -- this is the literal number the
    free-vs-paid decision was deliberately parked on: "after we see 10+
    emails come back for a 2nd check in 14 days, pricing solves a real
    problem." Before the email-gate removal this was mostly unmeasurable
    (a web check only ever carried a sender if that browser's email was
    already captured); now every opted-in web check carries one too, same
    as the email-forward channel, so this is the first point this number
    can be trusted."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT metadata, created_at FROM events WHERE event_type = 'tc_check'")
    rows = cursor.fetchall()
    conn.close()

    import json
    from collections import defaultdict
    by_sender = defaultdict(list)
    for metadata_json, created_at in rows:
        metadata = json.loads(metadata_json) if metadata_json else {}
        if "reason" in metadata:
            continue  # no_pdf / unreadable -- not an actual file checked
        sender = (metadata.get("sender") or "").strip().lower()
        if sender:
            by_sender[sender].append(created_at)

    returned = []
    for sender, timestamps in by_sender.items():
        timestamps.sort()
        first = datetime.fromisoformat(timestamps[0])
        for ts in timestamps[1:]:
            if (datetime.fromisoformat(ts) - first).days <= within_days:
                returned.append(sender)
                break

    return {
        "window_days": within_days,
        "distinct_senders": len(by_sender),
        "returned_within_window": len(returned),
        "senders": sorted(returned),
    }


def get_recent_tc_check_email_senders(limit: int = 20) -> list:
    """Most recent TC File Check email-forward events with the sender's
    actual address -- the raw log get_tc_check_summary()'s aggregates
    can't answer (e.g. "did any of these 9 specific people forward a
    file?"). Only the email channel carries a sender; web uploads never
    set 'source', so this filters to source == 'email' in Python rather
    than SQL since metadata is a JSON blob column."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT metadata, created_at, event_type FROM events
        WHERE event_type IN ('tc_check', 'tc_check_email_rejected')
        ORDER BY created_at DESC
        LIMIT 200
    """)
    rows = cursor.fetchall()
    conn.close()

    import json
    out = []
    for metadata_json, created_at, event_type in rows:
        metadata = json.loads(metadata_json) if metadata_json else {}
        dropped = event_type == "tc_check_email_rejected"
        if not dropped and metadata.get("source") != "email":
            continue
        out.append({
            "sender": metadata.get("sender", "(not recorded)"),
            "known_sender": metadata.get("known_sender", False),
            "recognized": metadata.get("recognized"),
            "reason": metadata.get("reason") or "",
            "dropped": dropped,
            "created_at": created_at,
        })
        if len(out) >= limit:
            break
    return out

def get_tc_check_bulk_summary(days: int = 30) -> dict:
    """Usage summary for Bulk TC File Check batches (/tc-check/bulk, see
    tc_bulk.py) -- deliberately separate from get_tc_check_summary()'s
    per-file numbers above. One 'tc_check_bulk' event already represents
    an entire batch (tc_bulk.process_batch fires exactly one per batch,
    not one per file inside it), so folding this into the single-check
    funnel would badly skew it: one 20-file free sample would look like
    20x the single-check traffic it actually represents.

    free_batches/brokerage_batches split by tier (tc_bulk.FREE_BULK_LIMIT
    vs MAX_BULK_FILES) -- answers the real question this metric exists
    for: is the free sample cap actually driving anyone to use a join
    code, or is every batch still on the free tier."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    cursor.execute("""
        SELECT metadata FROM events
        WHERE event_type = 'tc_check_bulk' AND created_at > ?
    """, (cutoff,))
    rows = cursor.fetchall()
    conn.close()

    import json
    batches = total_files = recognized = with_issues = 0
    free_batches = brokerage_batches = 0
    for row in rows:
        metadata = json.loads(row[0]) if row[0] else {}
        batches += 1
        total_files += metadata.get("total_files", 0)
        recognized += metadata.get("recognized_count", 0)
        with_issues += metadata.get("with_issues_count", 0)
        if metadata.get("tier") == "brokerage":
            brokerage_batches += 1
        else:
            free_batches += 1

    return {
        "batches": batches,
        "total_files": total_files,
        "recognized": recognized,
        "with_issues": with_issues,
        "with_issues_pct": round(with_issues / recognized * 100, 1) if recognized else 0,
        "free_batches": free_batches,
        "brokerage_batches": brokerage_batches,
    }


def get_signups_by_source(days: int = 30) -> list:
    """Signup counts grouped by ?src= attribution (Direct Reach, BiggerPockets,
    LinkedIn, etc.), most recent-heavy channels first. 'direct' covers anyone
    who signed up without an src param (e.g. typed the URL directly)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    cursor.execute("""
        SELECT metadata FROM events
        WHERE event_type = 'signup' AND created_at > ?
    """, (cutoff,))
    rows = cursor.fetchall()
    conn.close()

    import json
    counts = {}
    for row in rows:
        metadata = json.loads(row[0]) if row[0] else {}
        source = metadata.get("source") or "direct"
        counts[source] = counts.get(source, 0) + 1

    return sorted(
        [{"source": source, "count": count} for source, count in counts.items()],
        key=lambda r: -r["count"]
    )

def get_signup_details(days: int = 30) -> list:
    """Raw signup events (phone/name/email/source/timestamp), most recent
    first -- the per-record view behind the aggregate counts in
    get_signups_by_source(). Diagnostic use only, not shown on /analytics."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    cursor.execute("""
        SELECT phone, metadata, created_at FROM events
        WHERE event_type = 'signup' AND created_at > ?
        ORDER BY created_at DESC
    """, (cutoff,))
    rows = cursor.fetchall()
    conn.close()

    import json
    results = []
    for phone, metadata, created_at in rows:
        m = json.loads(metadata) if metadata else {}
        results.append({
            "phone": phone,
            "name": m.get("name"),
            "email": m.get("email"),
            "source": m.get("source") or "direct",
            "created_at": created_at,
        })
    return results

def get_waitlist_signups(limit: int = 200) -> list:
    """All waitlist signups, most recent first -- grouped by state on
    /analytics so demand for a specific state is visible at a glance."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT phone, metadata, created_at FROM events
        WHERE event_type = 'waitlist_joined'
        ORDER BY created_at DESC LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    import json
    results = []
    for row in rows:
        metadata = json.loads(row[1]) if row[1] else {}
        results.append({
            "phone": row[0],
            "state": metadata.get("state") or "Unknown",
            "created_at": row[2],
        })
    return results


def get_brokerage_alert_delivery(days: int = 30) -> dict:
    """Brokerage Alert delivery (email always, SMS on a blocker + tc_phone
    set) grouped by ?src= attribution -- added 2026-09-11, same day the SMS
    channel shipped, specifically to answer "are the cold-outreach leads
    (e.g. zillow_broker_reach) that became brokerages actually getting
    texted, or just the pre-existing email alert". Every 'brokerage_alert_sent'
    event already carries its own source/has_tc_phone/sms_sent fields (see
    _notify_brokerage_tc() in app.py) so this only needs to aggregate, not
    join against the brokerages table.

    Returns {"by_source": [...], "brokerages_with_sms": int,
    "brokerages_missing_phone": int} -- the latter two are brokerage counts
    (not event counts), since "how many distinct brokerages could add a
    phone and start getting texted" is the more actionable number than a
    raw event tally."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    cursor.execute("""
        SELECT metadata FROM events
        WHERE event_type = 'brokerage_alert_sent' AND created_at > ?
    """, (cutoff,))
    rows = cursor.fetchall()
    conn.close()

    import json
    by_source = {}
    brokerages_seen = {}  # brokerage_id -> (source, has_tc_phone) -- last value wins, fine either way
    for row in rows:
        m = json.loads(row[0]) if row[0] else {}
        source = m.get("source") or "direct"
        bucket = by_source.setdefault(source, {
            "source": source, "alerts": 0, "sms_sent": 0, "sms_eligible_no_phone": 0, "email_sent": 0,
        })
        bucket["alerts"] += 1
        if m.get("email_sent"):
            bucket["email_sent"] += 1
        if m.get("sms_sent"):
            bucket["sms_sent"] += 1
        elif m.get("sms_eligible") and not m.get("has_tc_phone"):
            bucket["sms_eligible_no_phone"] += 1
        brokerage_id = m.get("brokerage_id")
        if brokerage_id is not None:
            brokerages_seen[brokerage_id] = bool(m.get("has_tc_phone"))

    return {
        "by_source": sorted(by_source.values(), key=lambda r: -r["alerts"]),
        "brokerages_with_sms": sum(1 for has_phone in brokerages_seen.values() if has_phone),
        "brokerages_missing_phone": sum(1 for has_phone in brokerages_seen.values() if not has_phone),
    }

init_analytics_tables()


def get_visitor_roles() -> dict:
    """Per-visitor journey for the "What brings you here?" experiment
    (added 2026-10-06): visitor -> role -> saw drop box -> upload ->
    recognized -> repeat, one row per answered role plus "No answer".
    Everything joins on the ta_vid cookie. Counted from the first
    role_shown on, so it only covers the experiment window; uploads only
    carry ta_vid from the same day. "Real browser" = ran the page's JS
    (js_ok), which most scanners/link-preview bots never do."""
    import json
    conn = sqlite3.connect(DB_PATH)
    try:
        first = conn.execute("""SELECT MIN(created_at) FROM events WHERE event_type = 'page_engagement'
                                AND metadata LIKE '%"role_shown"%'""").fetchone()[0]
        if not first:
            return {"since": None}
        rows = conn.execute("""SELECT event_type, metadata FROM events WHERE created_at >= ? AND event_type IN
                               ('page_view', 'page_engagement', 'tc_check_attempted', 'tc_check_demo_used', 'tc_check')
                               ORDER BY created_at""", (first,)).fetchall()
    finally:
        conn.close()
    internal = _internal_visitor_ids()
    visitors, real, shown, dismissed, dropzone, demoed, uploaded = set(), set(), set(), set(), set(), set(), set()
    answer, recognized = {}, {}
    for event_type, metadata_json in rows:
        m = json.loads(metadata_json) if metadata_json else {}
        v = m.get("visitor") or ""
        if not v or v in internal:
            continue
        t = m.get("type", "")
        if event_type == "page_view":
            visitors.add(v)
        elif event_type == "tc_check_attempted":
            uploaded.add(v)
        elif event_type == "tc_check_demo_used":
            demoed.add(v)
        elif event_type == "tc_check":
            if m.get("source") != "email" and m.get("recognized"):
                recognized[v] = recognized.get(v, 0) + 1
        elif t == "js_ok":
            real.add(v)
        elif t == "dropzone_seen":
            dropzone.add(v)
        elif t == "role_shown":
            shown.add(v)
        elif t == "role_dismissed":
            dismissed.add(v)
        elif t.startswith("role_"):
            answer[v] = t[5:]
    # A visitor's page_view lands a few seconds before their own
    # role_shown, so the very first one predates `first`.
    visitors |= shown
    real |= shown
    labels = [("tc", "Transaction Coordinator"), ("agent", "Real Estate Agent"), ("broker", "Broker"),
              ("investor", "Investor"), ("browsing", "Just checking it out"), (None, "No answer")]
    total = len(answer)
    roles = []
    for key, label in labels:
        who = {v for v, r in answer.items() if r == key} if key else real - set(answer)
        roles.append({"key": key or "none", "label": label, "count": len(who),
                      "pct": round(100 * len(who) / total) if total and key else None,
                      "dropzone": len(who & dropzone), "demoed": len(who & demoed),
                      "uploaded": len(who & uploaded),
                      "recognized": sum(1 for v in who if recognized.get(v)),
                      "repeat": sum(1 for v in who if recognized.get(v, 0) >= 2)})
    return {"since": first[:10], "visitors": len(visitors), "real": len(real), "shown": len(shown),
            "answered": total, "dismissed": len(dismissed - set(answer)), "roles": roles}


def get_engagement_by_device(days: int = 7) -> list:
    """Sanity check that the engagement beacon fires on phones too (added
    2026-10-06): if mobile shows page views but ~0 of every JS event while
    desktop doesn't, it's a tracking bug, not visitor behavior."""
    import json
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute("""SELECT event_type, metadata FROM events WHERE created_at > ?
                               AND event_type IN ('page_view', 'page_engagement')""", (cutoff,)).fetchall()
    finally:
        conn.close()
    internal = _internal_visitor_ids()
    keys = ("js_ok", "stay_10s", "dropzone_seen", "hero_cta")
    out = {d: {"device": d, "visitors": set(), **{k: set() for k in keys}} for d in ("desktop", "mobile")}
    for event_type, metadata_json in rows:
        m = json.loads(metadata_json) if metadata_json else {}
        v, d = m.get("visitor") or "", out.get(m.get("device"))
        if not v or v in internal or d is None:
            continue
        if event_type == "page_view":
            d["visitors"].add(v)
        elif m.get("type") in keys:
            d[m["type"]].add(v)
    return [{k: (len(x) if isinstance(x, set) else x) for k, x in row.items()} for row in out.values()]


def get_recent_email_send_failures(limit: int = 10) -> list:
    """Outbound emails SendGrid refused (see integrations._record_send_failure)."""
    import json
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute("""SELECT metadata, created_at FROM events WHERE event_type = 'email_send_failed'
                               ORDER BY created_at DESC LIMIT ?""", (limit,)).fetchall()
    finally:
        conn.close()
    out = []
    for metadata_json, created_at in rows:
        m = json.loads(metadata_json) if metadata_json else {}
        out.append({"time": created_at, "to": m.get("to", ""), "subject": m.get("subject", ""), "error": m.get("error", "")})
    return out
