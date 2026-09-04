"""
analytics.py — Track key conversion metrics
"""
import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.environ.get("DATABASE_PATH", "subscriptions.db")

def init_analytics_tables():
    """Create analytics tables"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

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

def track_event(event_type: str, phone: str = None, metadata: dict = None):
    """Track an analytics event"""
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
}

def get_tc_check_summary(days: int = 30) -> dict:
    """Usage + funnel summary for the TC file-check tool (/v1/tc/check).
    Separate from the rest of this module's metrics -- that endpoint
    tracks 'tc_check' / 'tc_check_gated' / 'tc_check_email_captured'
    events (see app.py's tc_check()) that nothing else here surfaces, so
    this was invisible on the dashboard until now.

    issue_frequency answers "what checks fire most" directly from
    production traffic -- each issue in tc_audit.py's CHECKED_FIELDS (plus
    the derived checks: Effective Date, initials, addendum consistency)
    carries a stable 'key' precisely so it can be tallied here instead of
    parsed back out of free-text messages. Percentages are of *recognized*
    uploads, since an unrecognized file can't fire any real check."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    cursor.execute("""
        SELECT metadata FROM events
        WHERE event_type = 'tc_check' AND created_at > ?
    """, (cutoff,))
    rows = cursor.fetchall()

    cursor.execute("""
        SELECT COUNT(*) FROM events
        WHERE event_type = 'tc_check_gated' AND created_at > ?
    """, (cutoff,))
    gated = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(*) FROM events
        WHERE event_type = 'tc_check_email_captured' AND created_at > ?
    """, (cutoff,))
    emails_captured = cursor.fetchone()[0]
    conn.close()

    import json
    total = len(rows)
    recognized = complete = 0
    web_count = email_count = email_known_sender = 0
    issue_counts = {}
    for row in rows:
        metadata = json.loads(row[0]) if row[0] else {}
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
        "gated": gated,
        "emails_captured": emails_captured,
        "gate_conversion_rate": round(emails_captured / gated * 100, 1) if gated else 0,
        "issue_frequency": issue_frequency,
        "web_count": web_count,
        "email_count": email_count,
        "email_known_sender": email_known_sender,
        "email_new_sender_pct": round((email_count - email_known_sender) / email_count * 100, 1) if email_count else 0,
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

init_analytics_tables()
